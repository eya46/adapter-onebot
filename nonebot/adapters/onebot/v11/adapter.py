import hmac
import json
import asyncio
from typing import Any, Optional, cast

from nonebot.typing import overrides
from nonebot.utils import escape_tag
from nonebot.drivers import (
    URL,
    Driver,
    Request,
    Response,
    WebSocket,
    ForwardDriver,
    ReverseDriver,
    HTTPServerSetup,
    WebSocketServerSetup,
)

from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot
from .config import Config
from .event import Event, LifecycleMetaEvent, get_event_model
from .utils import ResultStore, log, get_auth_bearer, _handle_api_result

RECONNECT_INTERVAL = 3.0


class Adapter(BaseAdapter):
    @overrides(BaseAdapter)
    def __init__(self, driver: Driver, **kwargs: Any):
        super().__init__(driver, **kwargs)
        self.onebot_config: Config = Config(**self.config.dict())
        self.setup()

    @classmethod
    @overrides(BaseAdapter)
    def get_name(cls) -> str:
        return "OneBot V11"

    def setup(self) -> None:
        if isinstance(self.driver, ReverseDriver):
            http_setup = HTTPServerSetup(
                URL("/onebot/v11/http"), "POST", self.get_name(), self._handle_http
            )
            self.setup_http_server(http_setup)
            http_setup = HTTPServerSetup(
                URL("/onebot/v11/"), "POST", self.get_name(), self._handle_http
            )
            self.setup_http_server(http_setup)

            ws_setup = WebSocketServerSetup(
                URL("/onebot/v11/ws"), self.get_name(), self._handle_ws
            )
            self.setup_websocket_server(ws_setup)

        if self.onebot_config.ws_urls:
            if not isinstance(self.driver, ForwardDriver):
                log(
                    "WARNING",
                    f"Current driver {self.config.driver} don't support forward connections! Ignored",
                )
            else:
                self.driver.on_startup(self.start_forward)

    @overrides(BaseAdapter)
    async def _call_api(self, bot: Bot, api: str, **data) -> Any:
        ...

    async def _handle_http(self, request: Request) -> Response:
        self_id = request.headers.get("x-self-id")

        # check self_id
        if not self_id:
            log("WARNING", "Missing X-Self-ID Header")
            return Response(400, content="Missing X-Self-ID Header")

        # check signature
        response = self._check_signature(request)
        if response is not None:
            return response

        # check access_token
        response = self._check_access_token(request)
        if response is not None:
            return response

        data = request.content
        if data is not None:
            json_data = json.loads(data)
            event = self.json_to_event(json_data)
            if event:
                bot = self.bots.get(self_id)
                if not bot:
                    bot = Bot(self, self_id)
                    self.bot_connect(bot)
                bot = cast(Bot, bot)
                asyncio.create_task(bot.handle_event(event))
        return Response(204)

    async def _handle_ws(self, websocket: WebSocket) -> None:
        self_id = websocket.request.headers.get("x-self-id")

        # check self_id
        if not self_id:
            log("WARNING", "Missing X-Self-ID Header")
            await websocket.close(1008, "Missing X-Self-ID Header")
            return
        elif self_id in self.bots:
            log("WARNING", f"There's already a bot {self_id}, ignored")
            await websocket.close(1008, "Duplicate X-Self-ID")
            return

        # check access_token
        response = self._check_access_token(websocket.request)
        if response is not None:
            content = cast(str, response.content)
            await websocket.close(1008, content)
            return

        await websocket.accept()
        bot = Bot(self, self_id)
        self.bot_connect(bot)

        try:
            while True:
                data = await websocket.receive()
                json_data = json.loads(data)
                event = self.json_to_event(json_data)
                if event:
                    asyncio.create_task(bot.handle_event(event))
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Error while process data from websocket"
                f"for bot {escape_tag(self_id)}.</bg #f8bbd0></r>",
                e,
            )
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
            self.bot_disconnect(bot)

    def _check_signature(self, request: Request) -> Optional[Response]:
        x_signature = request.headers.get("x-signature")

        secret = self.onebot_config.secret
        if secret:
            if not x_signature:
                log("WARNING", "Missing Signature Header")
                return Response(401, content="Missing Signature", request=request)

            if request.content is None:
                return Response(400, content="Missing Content", request=request)

            body: bytes = (
                request.content
                if isinstance(request.content, bytes)
                else request.content.encode("utf-8")
            )
            sig = hmac.new(secret.encode("utf-8"), body, "sha1").hexdigest()
            if x_signature != "sha1=" + sig:
                log("WARNING", "Signature Header is invalid")
                return Response(403, content="Signature is invalid")

    def _check_access_token(self, request: Request) -> Optional[Response]:
        token = get_auth_bearer(request.headers.get("authorization"))

        access_token = self.onebot_config.access_token
        if access_token and access_token != token:
            msg = (
                "Authorization Header is invalid"
                if token
                else "Missing Authorization Header"
            )
            log(
                "WARNING",
                msg,
            )
            return Response(
                403,
                content=msg,
            )

    async def start_forward(self) -> None:
        for url in self.onebot_config.ws_urls:
            try:
                ws_url = URL(url)
                asyncio.create_task(self._forward_ws(ws_url))
            except Exception as e:
                log(
                    "ERROR",
                    f"<r><bg #f8bbd0>Bad url {escape_tag(url)} "
                    "in onebot forward websocket config</bg #f8bbd0></r>",
                    e,
                )

    async def _forward_ws(self, url: URL) -> None:
        headers = {}
        if self.onebot_config.access_token:
            headers["Authorization"] = f"Bearer {self.onebot_config.access_token}"
        request = Request("GET", url, headers=headers)

        bot: Optional[Bot] = None

        while True:
            try:
                ws = await self.websocket(request)
            except Exception as e:
                log(
                    "ERROR",
                    "<r><bg #f8bbd0>Error while setup websocket to "
                    f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                    e,
                )
                await asyncio.sleep(RECONNECT_INTERVAL)
                continue

            try:
                while True:
                    try:
                        data = await ws.receive()
                        json_data = json.loads(data)
                        event = self.json_to_event(json_data)
                        if not event:
                            continue
                        if not bot:
                            if (
                                not isinstance(event, LifecycleMetaEvent)
                                or event.sub_type != "connect"
                            ):
                                continue
                            self_id = event.self_id
                            bot = Bot(self, str(self_id))
                            self.bot_connect(bot)
                        asyncio.create_task(bot.handle_event(event))
                    except Exception as e:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        log(
                            "ERROR",
                            "<r><bg #f8bbd0>Error while process data from websocket"
                            f"{escape_tag(str(url))}. Trying to reconnect...</bg #f8bbd0></r>",
                            e,
                        )
                        break
            finally:
                if bot:
                    self.bot_disconnect(bot)
                    bot = None

            await asyncio.sleep(RECONNECT_INTERVAL)

    @classmethod
    def json_to_event(cls, json_data: Any) -> Optional[Event]:
        if not isinstance(json_data, dict):
            return None

        if "post_type" not in json_data:
            ResultStore.add_result(json_data)
            return

        try:
            post_type = json_data["post_type"]
            detail_type = json_data.get(f"{post_type}_type")
            detail_type = f".{detail_type}" if detail_type else ""
            sub_type = json_data.get("sub_type")
            sub_type = f".{sub_type}" if sub_type else ""
            models = get_event_model(post_type + detail_type + sub_type)
            for model in models:
                try:
                    event = model.parse_obj(json_data)
                    break
                except Exception as e:
                    log("DEBUG", "Event Parser Error", e)
            else:
                event = Event.parse_obj(json_data)

            return event
        except Exception as e:
            log(
                "ERROR",
                "<r><bg #f8bbd0>Failed to parse event. "
                f"Raw: {escape_tag(str(json_data))}</bg #f8bbd0></r>",
                e,
            )