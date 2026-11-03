"""Security regression tests for verified OIDC identity and physical tenant partitioning."""
from dataclasses import replace
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import main
from app.models import WorkflowCapture
from app.security import OIDCVerifier


ISSUER = "https://identity.example.test"
AUDIENCE = "aiops-secure-api"
JWKS = ISSUER + "/.well-known/jwks.json"
SCOPES = "aiops:read aiops:ingest aiops:execute aiops:replay aiops:audit aiops:admin"


@pytest.fixture
def secure_api(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "trusted-key", "use": "sig", "alg": "RS256"})

    verifier = OIDCVerifier(ISSUER, AUDIENCE, JWKS)
    monkeypatch.setattr(verifier.keys, "fetch_data", lambda: {"keys": [public_jwk]})
    monkeypatch.setattr(main, "verifier", verifier)
    monkeypatch.setattr(main, "settings", replace(
        main.settings, secure_mode=True, demo_mode=False, storage="sqlite",
        sqlite_path=str(tmp_path / "secure.db"),
        oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_jwks_url=JWKS,
        secure_allowed_providers="mock",
    ))
    main._tenant_services.clear()

    def token(tenant="alpha", subject="alice", scope=SCOPES, **overrides):
        now = int(time.time())
        payload = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": subject,
            "tenant_id": tenant, "scope": scope,
            "exp": now + 3600, "iat": now,
            "team": "identity-team", "application": "trusted-app",
        }
        payload.update(overrides)
        return jwt.encode(
            payload, private_key, algorithm="RS256",
            headers={"kid": "trusted-key"},
        )

    client = TestClient(main.app)
    yield client, token
    client.close()
    main._tenant_services.clear()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_missing_expired_wrong_audience_and_forged_tokens_are_rejected(secure_api):
    client, token = secure_api
    assert client.get("/v1/events").status_code == 401
    assert client.get("/v1/events", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/v1/events", headers=headers(token(exp=int(time.time()) - 500))).status_code == 401
    assert client.get("/v1/events", headers=headers(token(aud="wrong-audience"))).status_code == 401
    assert client.get("/v1/events", headers=headers(token(iss="https://evil.example"))).status_code == 401
    assert client.get("/v1/events", headers=headers(token(tenant_id="../escape"))).status_code == 401
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({
        "iss": ISSUER, "aud": AUDIENCE, "sub": "attacker",
        "tenant_id": "alpha", "scope": SCOPES,
        "exp": int(time.time()) + 3600,
    }, attacker, algorithm="RS256", headers={"kid": "trusted-key"})
    assert client.get("/v1/events", headers=headers(forged)).status_code == 401


def test_scopes_and_demo_seed_are_fail_closed(secure_api):
    client, token = secure_api
    read = headers(token(scope="aiops:read"))
    assert client.get("/v1/events", headers=read).status_code == 200
    assert client.post("/v1/gateway/generate", headers=read, json={
        "user_id": "claimed-other", "team": "someone-else",
        "application": "agent", "workflow": "summarize", "prompt": "hi",
    }).status_code == 403
    assert client.post("/v1/policies", headers=read, json={
        "name": "not allowed", "mode": "enforce",
    }).status_code == 403
    assert client.get("/v1/audit", headers=read).status_code == 403
    assert client.post("/v1/demo/seed", headers=headers(token())).status_code == 403
    assert client.post("/v1/gateway/generate", headers=headers(token()), json={
        "user_id": "a", "team": "b", "application": "c",
        "workflow": "x", "prompt": "private",
        "provider": "openai_compatible",
    }).status_code == 403


def test_verified_identity_replaces_client_claims_and_tenants_are_isolated(secure_api):
    client, token = secure_api
    a = headers(token(tenant="alpha", subject="alice"))
    b = headers(token(tenant="beta", subject="bob"))

    response = client.post("/v1/events", headers=a, json={
        "event_id": "evt_spoofed", "user_id": "admin", "team": "wrong-team",
        "application": "wrong-app", "agent": "wrong-agent",
        "workflow": "triage", "provider": "mock",
        "model": "premium-model", "input_tokens": 120,
        "output_tokens": 12, "estimated_cost_usd": 500000,
        "data_classification": "public",
        "metadata": {"secret": "CANARY_REQUEST_METADATA"},
    })
    assert response.status_code == 200
    assert response.json()["event_id"] != "evt_spoofed"
    a_events = client.get("/v1/events", headers=a).json()
    assert len(a_events) == 1
    event = a_events[0]
    assert event["user_id"] == "alice"
    assert event["team"] == "identity-team"
    assert event["application"] == "trusted-app"
    assert event["agent"] == "trusted-app"
    assert event["metadata"] == {}
    assert event["data_classification"] == "restricted"
    assert event["estimated_cost_usd"] < 1000
    assert len(client.get("/v1/events", headers=b).json()) == 0
    assert client.get("/v1/analytics/overview", headers=a).json()["calls"] == 1
    assert client.get("/v1/analytics/overview", headers=b).json()["calls"] == 0

    assert client.post("/v1/policies", headers=a, json={
        "name": "Only alpha", "mode": "shadow",
        "scope": {}, "rules": {"allowed_providers": ["mock"]},
    }).status_code == 200
    assert len(client.get("/v1/policies", headers=a).json()) == 1
    assert client.get("/v1/policies", headers=b).json() == []

    assert client.post("/v1/budgets", headers=a, json={
        "scope_type": "team", "scope_value": "identity-team",
        "monthly_limit_usd": 500,
    }).status_code == 200
    assert len(client.get("/v1/budgets", headers=a).json()) == 1
    assert client.get("/v1/budgets", headers=b).json() == []
    assert len(client.get("/v1/audit", headers=a).json()) == 2
    assert client.get("/v1/audit", headers=b).json() == []

    alpha_repo = main._tenant_services["alpha"].repo
    alpha_repo.insert_workflow(WorkflowCapture(
        workflow_id="wf_test_isolated", name="secret",
        prompt="Private context for alpha", context="SECRET_ALPHA_CAPTURE",
        baseline_provider="mock", baseline_model="premium-model",
        baseline_output="yes", baseline_input_tokens=50,
        baseline_output_tokens=5, baseline_cost_usd=0.01,
    ).model_dump())
    assert client.get("/v1/workflows/wf_test_isolated", headers=a).status_code == 200
    assert client.get("/v1/workflows/wf_test_isolated", headers=b).status_code == 404

    assert main._tenant_services["alpha"].repo.path != main._tenant_services["beta"].repo.path


def test_secure_gateway_never_captures_raw_prompt_or_untrusted_identity(secure_api):
    client, token = secure_api
    a = headers(token())
    response = client.post("/v1/gateway/generate", headers=a, json={
        "user_id": "spoofed-person", "team": "spoofed-team",
        "application": "spoofed-application", "agent": "spoofed-agent",
        "workflow": "classification", "prompt": "CONFIDENTIAL_PROMPT_CANARY",
        "context": "CONFIDENTIAL_CONTEXT_CANARY",
        "data_classification": "public",
        "capture_for_replay": True, "provider": "mock",
        "model": "standard-model", "metadata": {"secret": "CONFIDENTIAL_METADATA_CANARY"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["workflow_capture_id"] is None
    assert client.get("/v1/workflows", headers=a).json() == []
    assert client.post("/v1/workflows/fake/replay", headers=a, json={}).status_code == 403
    events = client.get("/v1/events", headers=a).json()
    assert events[0]["user_id"] == "alice"
    assert events[0]["team"] == "identity-team"
    assert events[0]["application"] == "trusted-app"
    assert events[0]["metadata"] == {}
    serialized = json.dumps(client.get("/v1/audit", headers=a).json())
    assert "CONFIDENTIAL_PROMPT_CANARY" not in serialized
    assert "CONFIDENTIAL_CONTEXT_CANARY" not in serialized
    assert "CONFIDENTIAL_METADATA_CANARY" not in serialized


def test_secured_mode_hides_dev_ui_and_sensitive_health_info(secure_api):
    client, token = secure_api
    for path in ("/", "/docs", "/redoc", "/openapi.json", "/static/index.html"):
        assert client.get(path).status_code == 404
    assert client.get("/health").json() == {"ok": True}

    response = client.get("/v1/events", headers=headers(token()))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_query_limits_block_unbounded_result_requests(secure_api):
    client, token = secure_api
    h = headers(token())
    assert client.get("/v1/events?limit=501", headers=h).status_code == 422
    assert client.get("/v1/audit?limit=99999", headers=h).status_code == 422
    assert client.get("/v1/replays?limit=201", headers=h).status_code == 422
    assert client.get("/v1/events?hours=-1", headers=h).status_code == 422
    assert client.get("/v1/events?limit=50", headers=h).status_code == 200


def test_oversized_content_length_and_chunked_request_are_rejected(secure_api):
    client, token = secure_api
    h = headers(token())
    payload = b'{"untrusted":"' + (b"x" * 270000) + b'"}'
    oversized = client.post("/v1/events", headers=h, content=payload)
    assert oversized.status_code == 413, oversized.text

    # No explicit Content-Length: emulate a chunked upload, which must be
    # interrupted during the receive stream, not buffered without a limit.
    def chunks():
        yield b'{"untrusted":"'
        yield b"y" * 150000
        yield b"z" * 150000
        yield b'"}'

    streamed = client.post("/v1/events", headers=h, content=chunks())
    assert streamed.status_code == 413, streamed.text


def test_secured_api_has_bounded_principal_and_source_rate_limits(secure_api, monkeypatch):
    from app.limits import FixedWindowLimiter

    client, token = secure_api
    monkeypatch.setattr(main, "_principal_limiter", FixedWindowLimiter(limit=1))
    h = headers(token())
    assert client.get("/v1/events", headers=h).status_code == 200
    limited = client.get("/v1/events", headers=h)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1

    monkeypatch.setattr(main, "_ip_limiter", FixedWindowLimiter(limit=1))
    assert client.get("/v1/events").status_code == 401
    unauth_limited = client.get("/v1/events")
    assert unauth_limited.status_code == 429
    assert int(unauth_limited.headers["retry-after"]) >= 1


def test_untrusted_policy_claims_and_untrusted_audit_fields_are_not_authoritative(secure_api):
    client, token = secure_api
    h = headers(token())
    response = client.post("/v1/events", headers=h, json={
        "user_id": "admin", "workflow": "triage",
        "provider": "mock", "model": "standard-model", "input_tokens": 80,
        "policy_decision": "allow", "policy_reasons": ["CANARY_FAKE_POLICY_REASON"],
        "error_code": "CANARY_FAKE_SECRET_ERROR",
        "session_id": "CANARY_SESSION_SECRET",
    })
    assert response.status_code == 200, response.text
    event = client.get("/v1/events", headers=h).json()[0]
    assert event["policy_decision"] == "unverified"
    assert event["policy_reasons"] == []
    assert event["error_code"] == ""
    assert event["session_id"] is None
    assert client.get("/v1/analytics/overview", headers=h).json()["policy_events"] == 0

    # An allowed gateway call stores no raw workflow/model identifiers in its
    # operational audit entry. It may keep per-tenant usage event metadata.
    result = client.post("/v1/gateway/generate", headers=h, json={
        "user_id": "other", "team": "other", "application": "other",
        "workflow": "CANARY_SECRET_WORKFLOW",
        "prompt": "Private request", "model": "CANARY_SECRET_MODEL",
        "provider": "mock",
    })
    assert result.status_code == 200, result.text

    # Denied calls and admin changes use sanitized audit fields too.
    assert client.post("/v1/policies", headers=h, json={
        "name": "CANARY_SECRET_POLICY_NAME", "mode": "enforce",
        "rules": {"allowed_providers": ["nonexistent-provider"]},
    }).status_code == 200
    denied = client.post("/v1/gateway/generate", headers=h, json={
        "user_id": "other", "team": "other", "application": "other",
        "workflow": "CANARY_SECRET_DENIED_WORKFLOW",
        "prompt": "Private denied request", "model": "CANARY_SECRET_DENIED_MODEL",
        "provider": "mock",
    })
    assert denied.status_code == 403

    assert client.post("/v1/budgets", headers=h, json={
        "scope_type": "application", "scope_value": "CANARY_SECRET_SCOPE",
        "monthly_limit_usd": 25,
    }).status_code == 200
    audits = client.get("/v1/audit", headers=h).json()
    serialized = json.dumps(audits)
    for marker in (
        "CANARY_SECRET_WORKFLOW", "CANARY_SECRET_MODEL",
        "CANARY_SECRET_DENIED_WORKFLOW", "CANARY_SECRET_DENIED_MODEL",
        "CANARY_SECRET_POLICY_NAME", "CANARY_SECRET_SCOPE",
        "CANARY_FAKE_SECRET_ERROR", "CANARY_FAKE_POLICY_REASON",
        "CANARY_SESSION_SECRET",
    ):
        assert marker not in serialized, marker


def test_malformed_and_excessive_numeric_inputs_are_rejected(secure_api):
    client, token = secure_api
    h = headers(token())
    assert client.post("/v1/events", headers=h, json={
        "model": "standard-model", "input_tokens": -1,
    }).status_code == 422
    assert client.post("/v1/events", headers=h, json={
        "model": "standard-model", "input_tokens": 10000001,
    }).status_code == 422
    assert client.post("/v1/gateway/generate", headers=h, json={
        "user_id": "u", "team": "t", "application": "a",
        "workflow": "w", "prompt": "hello", "max_output_tokens": 9999999,
    }).status_code == 422
    assert client.post("/v1/budgets", headers=h, json={
        "scope_type": "team", "scope_value": "x", "monthly_limit_usd": -5,
    }).status_code == 422
