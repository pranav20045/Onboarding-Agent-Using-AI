"""Per-IP rate limiting and request-body size limits.

The rate limiter is an in-process sliding window: fine for the single-process demo deployment.
Behind several workers or a load balancer it would need shared state (e.g. Redis) and a
trusted X-Forwarded-For; both limitations are documented in the README.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_body


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, window_s: float = 60.0) -> float | None:
        """Record a hit; return seconds to wait if over the limit, else None."""
        now = time.monotonic()
        q = self._hits[(bucket, key)]
        while q and now - q[0] >= window_s:
            q.popleft()
        if len(q) >= limit:
            return window_s - (now - q[0])
        q.append(now)
        return None


class BodySizeLimit:
    """Reject bodies over the limit (Content-Length check + streaming byte count)."""

    def __init__(self, app: ASGIApp, default_limit: int, overrides: dict[str, int]) -> None:
        self.app = app
        self.default_limit = default_limit
        self.overrides = overrides

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.overrides.get(scope["path"], self.default_limit)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            await _reject(send, limit)
            return
        seen = 0
        responded = False

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not responded:
                await _reject(send, limit)


class _TooLarge(Exception):
    pass


async def _reject(send: Send, limit: int) -> None:
    import json

    body = json.dumps(
        error_body("payload_too_large", f"Request body exceeds {limit} bytes")
    ).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})  # fmt: skip
    await send({"type": "http.response.body", "body": body})
