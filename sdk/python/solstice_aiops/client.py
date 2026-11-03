from __future__ import annotations

from typing import Callable
import httpx


class AIopsClient:
    """Lightweight Python instrumentation client.

    Pass a trusted OIDC bearer token for secure mode, or a callable token_provider
    to retrieve refreshed short-lived tokens before each API operation.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        timeout: int = 20,
        bearer_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._token_provider = token_provider
        self.client = httpx.Client(timeout=timeout, transport=transport)

    def _headers(self) -> dict[str, str]:
        token = self._token_provider() if self._token_provider else self._bearer_token
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def emit(self, **event):
        r = self.client.post(
            self.base_url + "/v1/events", json=event, headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    def generate(self, **request):
        r = self.client.post(
            self.base_url + "/v1/gateway/generate",
            json=request, headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def overview(self, hours: int = 24):
        r = self.client.get(
            self.base_url + "/v1/analytics/overview",
            params={"hours": hours}, headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    def close(self):
        self.client.close()
