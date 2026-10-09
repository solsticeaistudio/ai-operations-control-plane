from fastapi.testclient import TestClient
from app.main import app
client=TestClient(app)

def test_health_and_seed():
    assert client.get('/health').status_code==200
    assert client.post('/v1/demo/seed?clear=true').status_code==200
    o=client.get('/v1/analytics/overview?hours=240').json()
    assert o['calls']>100 and o['active_users']>1

def test_gateway_records_event():
    body={"user_id":"u","team":"Support","application":"test","workflow":"summary","prompt":"Summarize this email","provider":"mock","model":"premium-model","task_complexity":"low"}
    r=client.post('/v1/gateway/generate',json=body)
    assert r.status_code==200
    assert r.json()['estimated_cost_usd']>=0
