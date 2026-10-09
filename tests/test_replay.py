from app.repository import SQLiteRepository
from app.economics import MidasEconomics
from app.providers import ProviderRegistry
from app.replay import DeltaReplayEngine
from app.config import settings
import tempfile

def test_replay_returns_recommendation():
    repo=SQLiteRepository(tempfile.mktemp(suffix=".db")); econ=MidasEconomics(settings.pricing_file); engine=DeltaReplayEngine(repo,ProviderRegistry(),econ)
    workflow={"workflow_id":"wf1","prompt":"Classify ticket","context":"SSO certificate issue","baseline_model":"premium-model","baseline_provider":"mock","baseline_output":'{"confidence": 0.94, "decision": "escalate", "model": "premium-model", "summary": "Escalate based on the supplied workflow context."}',"baseline_cost_usd":econ.estimate_cost("premium-model",8000,300),"expected":{"decision":"escalate"},"metadata":{"difficulty":.45}}
    out=engine.compare(workflow,["economy-model","standard-model","premium-model"],"mock",.90)
    assert len(out["results"])==3
    assert out["recommendation"] is not None
