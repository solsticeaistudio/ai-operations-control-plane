"""The SDK must supply a bearer token for the secured API (including refresh)."""
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
from solstice_aiops import AIopsClient


def test_bearer_header_on_all_sdk_requests():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    client = AIopsClient(
        "https://api.example.test", bearer_token="signed-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.emit(workflow="test")["ok"]
        assert client.generate(prompt="hello")["ok"]
        assert client.overview()["ok"]
    finally:
        client.close()

    assert [auth for _, auth in seen] == ["Bearer signed-token"] * 3
    assert [path for path, _ in seen] == [
        "/v1/events", "/v1/gateway/generate", "/v1/analytics/overview",
    ]


def test_sdk_token_provider_refreshes_per_request():
    tokens = iter(["fresh-token-1", "fresh-token-2"])
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"ok": True})

    client = AIopsClient(
        "https://api.example.test",
        token_provider=lambda: next(tokens),
        transport=httpx.MockTransport(handler),
    )
    try:
        client.emit(workflow="w")
        client.overview()
    finally:
        client.close()

    assert seen == ["Bearer fresh-token-1", "Bearer fresh-token-2"]
