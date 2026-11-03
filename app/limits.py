"""Bounded, process-local request controls for the secured API.

This is defense in depth for one service process, not a substitute for a shared
ingress rate limiter, quotas, WAF, or global distributed abuse protection.
"""
from __future__ import annotations

from math import ceil
from threading import Lock
import time

from starlette.responses import JSONResponse


class FixedWindowLimiter:
    def __init__(self, limit: int, period_seconds: int = 60, max_keys: int = 10000):
        if limit < 1 or period_seconds < 1 or max_keys < 1:
            raise ValueError("Invalid rate-limit configuration")
        self.limit = limit
        self.period = period_seconds
        self.max_keys = max_keys
        self._state: dict[str, tuple[float, int]] = {}
        self._lock = Lock()

    def check(self, key: str) -> int:
        """Return 0 if allowed, otherwise the Retry-After value in seconds."""
        now = time.monotonic()
        with self._lock:
            window = self._state.get(key)
            if window and window[0] > now:
                if window[1] >= self.limit:
                    return max(1, ceil(window[0] - now))
                self._state[key] = (window[0], window[1] + 1)
                return 0

            if key not in self._state and len(self._state) >= self.max_keys:
                # Bounded memory: remove expired windows before admitting new keys.
                self._state = {
                    name: value for name, value in self._state.items()
                    if value[0] > now
                }
                if len(self._state) >= self.max_keys:
                    return self.period

            self._state[key] = (now + self.period, 1)
            return 0


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject oversized /v1/ POST/PATCH/PUT bodies, including chunked requests.

    The receive wrapper counts bytes *as the ASGI body is consumed*, so a large
    chunked upload cannot force the application to buffer unlimited request data.
    """

    def __init__(self, app, max_bytes: int, secure_mode):
        self.app = app
        self.max_bytes = max_bytes
        self.secure_mode = secure_mode

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not self.secure_mode() or
                not scope.get("path", "").startswith("/v1/") or
                scope.get("method") not in {"POST", "PUT", "PATCH"}):
            await self.app(scope, receive, send)
            return

        # Reject an over-limit Content-Length without reading the body.
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    announced = int(value)
                except ValueError:
                    announced = -1
                if announced < 0:
                    await self._reject(scope, receive, send, 400, "Invalid Content-Length")
                    return
                if announced > self.max_bytes:
                    await self._reject(scope, receive, send, 413, "Request body too large")
                    return

        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            msg = await receive()
            if msg["type"] == "http.request":
                consumed += len(msg.get("body", b""))
                if consumed > self.max_bytes:
                    raise BodyTooLarge()
            return msg

        async def tracked_send(msg):
            nonlocal response_started
            if msg["type"] == "http.response.start":
                response_started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, 413, "Request body too large")

    @staticmethod
    async def _reject(scope, receive, send, code: int, reason: str):
        response = JSONResponse(
            status_code=code, content={"detail": reason},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        await response(scope, receive, send)
