import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from solstice_aiops import AIopsClient
from app.main import app


def _client():
    c = AIopsClient("http://testserver")
    # TestClient is an httpx.Client, so it drops into the SDK unchanged and
    # exercises the real SDK request/response path (raise_for_status + .json()).
    c.client = TestClient(app)
    return c


def test_sdk_emit_accepts_ledger_response_shape():
    out = _client().emit(
        event_id="sdk_emit_1", user_id="sdk-user", team="Support",
        application="sdk-app", workflow="sdk-wf", provider="mock",
        model="standard-model", input_tokens=10, output_tokens=2,
        estimated_cost_usd=0.02,
    )
    # emit() returns the parsed response; the response must remain valid and the
    # SDK must not depend on the ledger object's internal shape.
    assert out["ok"] is True
    assert out["event_id"] == "sdk_emit_1"
    assert out["ledger"]["ledger_protected"] is True
    assert out["ledger"]["created"] in (0, 1)


def test_sdk_generate_still_works():
    out = _client().generate(
        user_id="sdk-user", team="Support", application="sdk-app",
        workflow="sdk-wf", prompt="Summarize this ticket", provider="mock",
        model="standard-model", task_complexity="low",
    )
    assert out["model"] and isinstance(out["estimated_cost_usd"], (int, float))


def test_sdk_overview_still_works():
    out = _client().overview(hours=240)
    assert "calls" in out
