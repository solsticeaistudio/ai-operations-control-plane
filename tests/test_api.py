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

def test_ledger_endpoints_backfill_seeded_spend():
    assert client.post('/v1/demo/seed?clear=true').status_code==200
    led=client.get('/v1/ledger?limit=5').json()
    assert led['count']>100 and len(led['entries'])==5
    v=client.get('/v1/ledger/verify').json()
    assert v['ok'] is True and v['entries']==led['count']
    h=client.get('/v1/ledger/head').json()
    assert h['count']==led['count'] and h['meta']['head_hash']

def test_gateway_writes_ledger_entry():
    client.post('/v1/demo/seed?clear=true')
    before=client.get('/v1/ledger').json()['count']
    body={"user_id":"ledger-u","team":"Support","application":"t","workflow":"summary","prompt":"Summarize this email","provider":"mock","model":"premium-model","task_complexity":"low"}
    assert client.post('/v1/gateway/generate',json=body).status_code==200
    after=client.get('/v1/ledger').json()
    assert after['count']==before+1
    assert after['entries'][0]['source']=='gateway:/v1/gateway/generate'
    assert after['entries'][0]['pricing_version']

def test_event_ingest_is_idempotent_in_ledger():
    ev={"event_id":"evt_idem_1","user_id":"u","team":"Support","application":"a","workflow":"w","model":"standard-model","input_tokens":100,"output_tokens":10}
    r1=client.post('/v1/events',json=ev).json()
    r2=client.post('/v1/events',json=ev).json()
    assert r1['ledger']['ledger_protected'] is True
    assert r1['ledger']['created']==1
    assert r2['ledger']['created']==0 and r2['ledger']['duplicates']==1
    assert client.get('/v1/ledger/verify').json()['ok'] is True

def test_existing_analytics_unaffected_by_ledger():
    client.post('/v1/demo/seed?clear=true')
    o=client.get('/v1/analytics/overview?hours=240').json()
    assert o['calls']>100 and o['cost_per_successful_task_usd'] is not None
    b=client.get('/v1/analytics/breakdown?group_by=team&hours=240').json()
    assert len(b)>0
    assert client.get('/v1/recommendations?hours=240').status_code==200
    assert client.get('/v1/budgets').status_code==200

def test_event_and_ledger_agree_after_ingest():
    client.post('/v1/demo/seed?clear=true')
    ev={"event_id":"evt_agree_1","user_id":"u","team":"Support","application":"a","workflow":"w","model":"standard-model","input_tokens":100,"output_tokens":10,"estimated_cost_usd":0.5}
    assert client.post('/v1/events',json=ev).json()['ledger']['ledger_protected'] is True
    r=client.get('/v1/ledger/reconcile').json()
    assert r['supported'] is True and r['ok'] is True
    assert r['missing_ledger_count']==0 and r['cost_mismatch_count']==0

def test_resubmitted_event_cannot_change_recorded_cost():
    ev={"event_id":"evt_drift_1","user_id":"u","team":"Support","application":"a","workflow":"w","model":"standard-model","input_tokens":100,"output_tokens":10,"estimated_cost_usd":0.5}
    first=client.post('/v1/events',json=ev).json()
    ev2=dict(ev); ev2["estimated_cost_usd"]=0.9
    second=client.post('/v1/events',json=ev2).json()
    assert first['ledger']['created']==1
    assert second['ledger']['created']==0 and second['ledger']['duplicates']==1
    rows=[e for e in client.get('/v1/events?hours=24&limit=5000').json() if e['event_id']=='evt_drift_1']
    assert len(rows)==1 and rows[0]['estimated_cost_usd']==0.5
    assert client.get('/v1/ledger/reconcile').json()['cost_mismatch_count']==0

def test_demo_reset_preserves_ledger_history():
    client.post('/v1/demo/seed?clear=true')
    before=client.get('/v1/ledger').json()
    assert before['count']>100
    client.post('/v1/demo/seed?clear=true')
    after=client.get('/v1/ledger').json()
    assert after['count']==before['count'], "demo reset must not delete retained ledger history"
    assert after['entries'][0]['entry_id']==before['entries'][0]['entry_id']
    assert client.get('/v1/ledger/verify').json()['ok'] is True

def test_reconcile_reports_no_spend_loss_on_seeded_data():
    client.post('/v1/demo/seed?clear=true')
    r=client.get('/v1/ledger/reconcile').json()
    assert r['ok'] is True
    assert r['matched']==r['events']
    assert r['missing_ledger_count']==0 and r['cost_mismatch_count']==0
    # retained ledger-only history is surfaced, not treated as a failure
    assert r['ledger_only_count']==r['ledger_entries']-r['matched']

def test_demo_reset_preserves_real_telemetry():
    real={"event_id":"real_prod_1","user_id":"prod-user","team":"Engineering","application":"prod-app","workflow":"prod-wf","provider":"mock","model":"standard-model","input_tokens":5000,"output_tokens":400,"estimated_cost_usd":1.25,"data_classification":"internal"}
    assert client.post('/v1/events',json=real).status_code==200
    assert client.post('/v1/demo/seed?clear=true').status_code==200
    rows=[e for e in client.get('/v1/events?hours=24&limit=5000').json() if e['event_id']=='real_prod_1']
    assert len(rows)==1, "real telemetry must survive a demo reset"
    assert rows[0]['estimated_cost_usd']==1.25 and rows[0]['team']=='Engineering'
    entry=client.get('/v1/ledger/spend_real_prod_1').json()
    assert entry['cost_usd']==1.25 and entry['payload']['team']=='Engineering'
    assert client.get('/v1/ledger/reconcile').json()['ok'] is True

def test_demo_reset_preserves_real_policies_and_budgets():
    client.post('/v1/policies',json={"name":"real operator policy","mode":"observe"})
    client.post('/v1/budgets',json={"scope_type":"team","scope_value":"Engineering","monthly_limit_usd":99.0})
    client.post('/v1/demo/seed?clear=true')
    names=[p['name'] for p in client.get('/v1/policies').json()]
    scopes=[b['scope_value'] for b in client.get('/v1/budgets').json()]
    assert 'real operator policy' in names, "real policy must survive demo reset"
    assert 'Engineering' in scopes, "real budget must survive demo reset"
    # the seeder's own demo policy is recreated by the seed run
    assert 'Enterprise provider & retry baseline' in names

def test_demo_operations_disabled_outside_demo_mode(monkeypatch):
    import dataclasses
    import app.main as m
    monkeypatch.setattr(m,"settings",dataclasses.replace(m.settings,demo_mode=False))
    assert client.post('/v1/demo/seed?clear=true').status_code==403
