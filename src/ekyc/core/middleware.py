import re
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ekyc.core.logging import request_id_ctx


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"x-request-id", b"").decode("latin-1")
        identifier = re.sub(r"[^A-Za-z0-9_-]", "", raw)[:64] or uuid4().hex
        scope.setdefault("state", {})["request_id"] = identifier
        token = request_id_ctx.set(identifier)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                message["headers"] = [*headers, (b"x-request-id", identifier.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_ctx.reset(token)
