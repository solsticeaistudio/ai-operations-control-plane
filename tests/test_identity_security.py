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
