from app.repository import SQLiteRepository
from app.economics import MidasEconomics
from app.policy import PolicyEngine
from app.config import settings
import tempfile

def test_shadow_does_not_block():
    repo=SQLiteRepository(tempfile.mktemp(suffix=".db")); m=MidasEconomics(settings.pricing_file); p=PolicyEngine(repo,m)
    repo.upsert_policy({"name":"restricted","mode":"shadow","scope":{},"rules":{"allowed_providers":["approved"]}})
    out=p.evaluate({"provider":"other","team":"x","application":"a","workflow":"w","user_id":"u","estimated_input_tokens":10,"retry_count":0,"tool_calls":0,"data_classification":"internal","task_complexity":"medium"})
    assert out["decision"]=="allow" and out["shadow_actions"]

def test_enforce_blocks():
    repo=SQLiteRepository(tempfile.mktemp(suffix=".db")); m=MidasEconomics(settings.pricing_file); p=PolicyEngine(repo,m)
    repo.upsert_policy({"name":"restricted","mode":"enforce","scope":{},"rules":{"allowed_providers":["approved"]}})
    out=p.evaluate({"provider":"other","team":"x","application":"a","workflow":"w","user_id":"u","estimated_input_tokens":10,"retry_count":0,"tool_calls":0,"data_classification":"internal","task_complexity":"medium"})
    assert out["decision"]=="deny"
