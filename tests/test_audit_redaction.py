"""Regression tests: audit records are metadata-only, never request-body copies."""
import json

from fastapi.testclient import TestClient

from app import main
from app.policy import PolicyEngine
from app.repository import SQLiteRepository


def isolated_client(tmp_path, monkeypatch):
    # Avoid contaminating the demo database or tests in other modules.
    repo = SQLiteRepository(str(tmp_path / "audit_redaction.db"))
    monkeypatch.setattr(main, "repo", repo)
    monkeypatch.setattr(main, "policy_engine", PolicyEngine(repo, main.economics))
    return TestClient(main.app), repo


def test_denied_gateway_request_never_copies_sensitive_body_to_audit(tmp_path, monkeypatch):
    client, repo = isolated_client(tmp_path, monkeypatch)
    repo.upsert_policy({
        "name": "Approved providers only",
        "mode": "enforce",
        "scope": {},
        "rules": {"allowed_providers": ["approved-provider"]},
    })
    response = client.post("/v1/gateway/generate", json={
        "user_id": "test-user",
        "team": "support",
        "application": "helpdesk",
        "workflow": "summarize",
        "provider": "mock",
        "model": "premium-model",
        "prompt": "AUDIT_TEST_PRIVATE_PROMPT_9e26",
        "context": "AUDIT_TEST_PRIVATE_CONTEXT_8b33",
        "metadata": {"private": "AUDIT_TEST_PRIVATE_METADATA_5c17"},
    })
    assert response.status_code == 403

    records = client.get("/v1/audit").json()
    denied = next(r for r in records if r["kind"] == "policy" and r["decision"] == "deny")
    details = denied["details"]
    assert details["provider"] == "mock"
    assert details["model"] == "premium-model"
    assert details["reasons"]
    assert "request" not in details
    serialized = json.dumps(records)
    for sentinel in (
        "AUDIT_TEST_PRIVATE_PROMPT_9e26",
        "AUDIT_TEST_PRIVATE_CONTEXT_8b33",
        "AUDIT_TEST_PRIVATE_METADATA_5c17",
    ):
        assert sentinel not in serialized


def test_policy_audit_stores_metadata_not_arbitrary_rules_or_scope(tmp_path, monkeypatch):
    client, _ = isolated_client(tmp_path, monkeypatch)
    response = client.post("/v1/policies", json={
        "name": "Security review policy",
        "mode": "shadow",
        "priority": 5,
        "scope": {"private_field": "AUDIT_TEST_PRIVATE_SCOPE_37a4"},
        "rules": {"notes": "AUDIT_TEST_PRIVATE_RULE_43dc"},
    })
    assert response.status_code == 200
    records = client.get("/v1/audit").json()
    change = next(r for r in records if r["kind"] == "policy" and r["decision"] == "upsert")
    assert change["details"] == {
        "name": "Security review policy",
        "mode": "shadow",
        "priority": 5,
        "enabled": True,
    }
    serialized = json.dumps(records)
    assert "AUDIT_TEST_PRIVATE_SCOPE_37a4" not in serialized
    assert "AUDIT_TEST_PRIVATE_RULE_43dc" not in serialized
