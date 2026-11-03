"""Verify untrusted values are passed as query parameters, not SQL text."""
import httpx

from app.repository import ClickHouseRepository


def test_clickhouse_workflow_identifier_is_a_bound_parameter():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text="")

    repo = ClickHouseRepository.__new__(ClickHouseRepository)
    repo.url = "https://clickhouse.example.test"
    repo.database = "aiops_t_abc"
    repo.auth = None
    repo.client = httpx.Client(transport=httpx.MockTransport(handle))

    try:
        payload = "wf_' OR 1=1 -- sensitive_test"
        assert repo.get_workflow(payload) is None
        assert repo.monthly_spend("team", payload) == 0.0
        assert repo.events(hours=12, limit=5) == []
        assert repo.audits(limit=10) == []
    finally:
        repo.client.close()

    workflow_req, spend_req, events_req, audits_req = requests
    assert payload not in workflow_req.url.params["query"]
    assert workflow_req.url.params["param_workflow_id"] == payload
    assert "{workflow_id:String}" in workflow_req.url.params["query"]
    assert payload not in spend_req.url.params["query"]
    assert spend_req.url.params["param_scope_value"] == payload
    assert "team={scope_value:String}" in spend_req.url.params["query"]
    assert events_req.url.params["param_hours"] == "12"
    assert events_req.url.params["param_limit"] == "5"
    assert audits_req.url.params["param_limit"] == "10"


def test_clickhouse_query_parameter_names_are_validated():
    repo = ClickHouseRepository.__new__(ClickHouseRepository)
    repo.url = "https://clickhouse.example.test"
    repo.database = "aiops_t_abc"
    repo.auth = None
    repo.client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="")
    ))
    try:
        try:
            repo._query_json("SELECT 1", {"malicious-name;x": "danger"})
            raise AssertionError("Invalid parameter name should have been refused")
        except ValueError:
            pass
    finally:
        repo.client.close()
