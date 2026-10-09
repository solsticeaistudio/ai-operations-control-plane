from __future__ import annotations
from pathlib import Path
from collections import Counter
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .config import settings
from .models import AIEvent, GatewayRequest, GatewayResponse, PolicyCreate, BudgetCreate, ReplayCompareRequest, WorkflowCapture
from .repository import create_repository
from .economics import MidasEconomics
from .providers import ProviderRegistry
from .policy import PolicyEngine
from .replay import DeltaReplayEngine
from .analytics import AnalyticsService
from .seed import seed_demo

repo=create_repository(settings)
economics=MidasEconomics(settings.pricing_file)
providers=ProviderRegistry(settings.openai_compat_base_url,settings.openai_compat_api_key)
policy_engine=PolicyEngine(repo,economics)
replay_engine=DeltaReplayEngine(repo,providers,economics)
analytics=AnalyticsService(repo,economics)

app=FastAPI(title="Solstice AI Operations Control Plane",version="0.1.0",description="Telemetry, model economics, replay experiments and guardrails for enterprise AI.")
static=Path(__file__).resolve().parent/"static"

@app.middleware("http")
async def optional_api_key_auth(request: Request, call_next):
    # Local demo runs open by default. Set AIOPS_API_KEY to protect product APIs.
    if settings.api_key and request.url.path.startswith("/v1/"):
        supplied=request.headers.get("x-aiops-key","")
        if supplied != settings.api_key:
            return JSONResponse(status_code=401,content={"detail":"Missing or invalid X-AIOps-Key"})
    return await call_next(request)

app.mount("/static",StaticFiles(directory=static),name="static")

@app.get("/")
def index(): return FileResponse(static/"index.html")

@app.get("/health")
def health(): return {"ok":True,"backend":repo.backend,"version":app.version,"pricing_version":economics.version,"providers":list(providers.providers)}

@app.post("/v1/demo/seed")
def demo_seed(clear: bool=True): return seed_demo(repo,economics,clear=clear)

@app.post("/v1/events")
def ingest(event: AIEvent):
    if not event.estimated_cost_usd: event.estimated_cost_usd=economics.estimate_cost(event.model,event.input_tokens,event.output_tokens,event.cached_tokens)
    repo.insert_event(event.model_dump()); return {"ok":True,"event_id":event.event_id,"estimated_cost_usd":event.estimated_cost_usd}

@app.post("/v1/events/batch")
def ingest_batch(events: list[AIEvent]):
    if len(events)>5000: raise HTTPException(413,"Batch limit is 5,000 events")
    ids=[]
    for event in events:
        if not event.estimated_cost_usd: event.estimated_cost_usd=economics.estimate_cost(event.model,event.input_tokens,event.output_tokens,event.cached_tokens)
        repo.insert_event(event.model_dump()); ids.append(event.event_id)
    return {"ok":True,"accepted":len(ids),"event_ids":ids}

@app.post("/v1/gateway/generate",response_model=GatewayResponse)
def generate(req: GatewayRequest):
    estimated_input=max(1,int(len((req.prompt+" "+req.context).split())*1.32))
    ctx=req.model_dump() | {"estimated_input_tokens":estimated_input}
    p=policy_engine.evaluate(ctx)
    if p["decision"]=="deny":
        repo.audit("policy","gateway",req.workflow,"deny",{"reasons":p["reasons"],"request":ctx})
        raise HTTPException(403,{"message":"Request denied by policy","reasons":p["reasons"]})
    selected=p.get("route_model") or req.model
    adapter=providers.get(req.provider)
    meta=dict(req.metadata); meta.setdefault("difficulty",{"low":.18,"medium":.45,"high":.72}[req.task_complexity])
    result=adapter.generate(selected,req.prompt,req.context,req.max_output_tokens,meta)
    cost=economics.estimate_cost(selected,result.input_tokens,result.output_tokens,result.cached_tokens)
    expected=req.metadata.get("expected") or {}
    quality=None; task_success=None
    if expected: quality,task_success=replay_engine.quality_score(result.output,result.output,expected)
    event=AIEvent(user_id=req.user_id,team=req.team,application=req.application,workflow=req.workflow,agent=req.agent,provider=req.provider,model=selected,input_tokens=result.input_tokens,output_tokens=result.output_tokens,cached_tokens=result.cached_tokens,latency_ms=result.latency_ms,estimated_cost_usd=cost,success=result.success,error_code=result.error_code,tool_calls=req.tool_calls,retry_count=req.retry_count,data_classification=req.data_classification,policy_decision=p["decision"],policy_reasons=p["reasons"],task_success=task_success,quality_score=quality,metadata=req.metadata)
    repo.insert_event(event.model_dump())
    capture_id=None
    if req.capture_for_replay:
        expected_obj=req.metadata.get("expected") or {}
        wf=WorkflowCapture(name=req.workflow,prompt=req.prompt,context=req.context,baseline_provider=req.provider,baseline_model=selected,baseline_output=result.output,baseline_input_tokens=result.input_tokens,baseline_output_tokens=result.output_tokens,baseline_cost_usd=cost,expected=expected_obj,metadata=meta)
        repo.insert_workflow(wf.model_dump()); capture_id=wf.workflow_id
    repo.audit("gateway",req.user_id,req.workflow,"allow",{"model_requested":req.model,"model_used":selected,"cost_usd":cost,"shadow_actions":p["shadow_actions"]})
    return GatewayResponse(request_id=event.request_id,trace_id=event.trace_id,provider=req.provider,model=selected,output=result.output,input_tokens=result.input_tokens,output_tokens=result.output_tokens,cached_tokens=result.cached_tokens,latency_ms=result.latency_ms,estimated_cost_usd=cost,policy_decision=p["decision"],policy_reasons=p["reasons"],shadow_actions=p["shadow_actions"],workflow_capture_id=capture_id)

@app.get("/v1/analytics/overview")
def overview(hours:int=Query(24,ge=1,le=24*365)): return analytics.overview(hours)
@app.get("/v1/analytics/breakdown")
def breakdown(group_by:str="team",hours:int=24,limit:int=20): return analytics.group(group_by,hours,limit)
@app.get("/v1/events")
def list_events(hours:int=24,limit:int=100): return repo.events(hours,limit)

@app.get("/v1/recommendations")
def recommendations(hours:int=24): return economics.recommendations(repo.events(hours,5000),repo.replay_runs())

@app.get("/v1/workflows")
def workflows(limit:int=100): return repo.list_workflows(limit)
@app.get("/v1/workflows/{workflow_id}")
def workflow(workflow_id:str):
    w=repo.get_workflow(workflow_id)
    if not w: raise HTTPException(404,"Workflow not found")
    return w
@app.post("/v1/workflows/{workflow_id}/replay")
def replay(workflow_id:str,req:ReplayCompareRequest):
    w=repo.get_workflow(workflow_id)
    if not w: raise HTTPException(404,"Workflow not found")
    out=replay_engine.compare(w,req.candidate_models,req.provider,req.min_quality_score); repo.audit("replay","operator",workflow_id,"completed",{"candidate_models":req.candidate_models,"recommendation":out.get("recommendation")}); return out
@app.get("/v1/replays")
def replays(limit:int=100): return repo.replay_runs(limit)

@app.post("/v1/policies")
def create_policy(p:PolicyCreate):
    pid=repo.upsert_policy(p.model_dump()); repo.audit("policy","operator",pid,"upsert",p.model_dump()); return {"ok":True,"policy_id":pid}
@app.get("/v1/policies")
def policies(): return repo.list_policies()
@app.post("/v1/budgets")
def create_budget(b:BudgetCreate):
    bid=repo.upsert_budget(b.model_dump()); repo.audit("budget","operator",bid,"upsert",b.model_dump()); return {"ok":True,"budget_id":bid}
@app.get("/v1/budgets")
def budgets():
    out=[]
    for b in repo.list_budgets():
        spend=repo.monthly_spend(b["scope_type"],b["scope_value"]); out.append({**b,"current_spend_usd":round(spend,4),"utilization":round(spend/max(float(b["monthly_limit_usd"]),1e-12),4)})
    return out
@app.get("/v1/audit")
def audit(limit:int=100): return repo.audits(limit)
