from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
from threading import Lock
from datetime import datetime, timezone
import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .models import (
    AIEvent, GatewayRequest, GatewayResponse, PolicyCreate, BudgetCreate,
    ReplayCompareRequest, WorkflowCapture,
)
from .repository import create_repository
from .economics import MidasEconomics
from .providers import ProviderRegistry
from .policy import PolicyEngine
from .replay import DeltaReplayEngine
from .analytics import AnalyticsService
from .security import OIDCVerifier, Principal
from .seed import seed_demo


if not settings.demo_mode and not settings.secure_mode:
    raise RuntimeError(
        "Refusing to run without security: enable AIOPS_SECURE_MODE or explicitly set AIOPS_DEMO_MODE=true"
    )

verifier = (
    OIDCVerifier(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    if settings.secure_mode else None
)
repo = None if settings.secure_mode else create_repository(settings)
economics = MidasEconomics(settings.pricing_file)
providers = ProviderRegistry(settings.openai_compat_base_url, settings.openai_compat_api_key)
policy_engine = PolicyEngine(repo, economics) if repo is not None else None
replay_engine = DeltaReplayEngine(repo, providers, economics) if repo is not None else None
analytics = AnalyticsService(repo, economics) if repo is not None else None


@dataclass
class Services:
    repo: object
    policy_engine: PolicyEngine
    replay_engine: DeltaReplayEngine
    analytics: AnalyticsService


_tenant_services: dict[str, Services] = {}
_tenant_lock = Lock()
_MAX_TENANTS_PER_PROCESS = 128


def services_for(request: Request, required_scope: str = "aiops:read") -> Services:
    if not settings.secure_mode:
        # Resolve at request time to preserve testing overrides and local-demo use.
        return Services(repo, policy_engine, replay_engine, analytics)

    principal: Principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not principal.permitted(required_scope):
        raise HTTPException(status_code=403, detail="Insufficient scope")

    with _tenant_lock:
        service = _tenant_services.get(principal.tenant_id)
        if service is None:
            if len(_tenant_services) >= _MAX_TENANTS_PER_PROCESS:
                raise HTTPException(status_code=503, detail="Tenant capacity exceeded")
            tenant_repo = create_repository(settings, tenant_id=principal.tenant_id)
            service = Services(
                tenant_repo,
                PolicyEngine(tenant_repo, economics),
                DeltaReplayEngine(tenant_repo, providers, economics),
                AnalyticsService(tenant_repo, economics),
            )
            _tenant_services[principal.tenant_id] = service
        return service


def authenticated_actor(request: Request) -> str:
    return request.state.principal.subject if settings.secure_mode else "operator"


def trusted_event(event: AIEvent, request: Request) -> AIEvent:
    if not settings.secure_mode:
        return event
    principal: Principal = request.state.principal
    # SDK metadata and identity fields are not proof of the caller's identity.
    # In secure mode the identity is bound to verified issuer-signed claims.
    # Treat source-classification and arbitrary metadata conservatively until
    # a trusted data-classification and metadata contract has been integrated.
    return event.model_copy(update={
        "event_id": f"evt_{secrets.token_hex(16)}",
        "request_id": f"req_{secrets.token_hex(12)}",
        "trace_id": f"tr_{secrets.token_hex(12)}",
        "timestamp": datetime.now(timezone.utc),
        "user_id": principal.subject,
        "team": principal.team,
        "application": principal.application,
        "agent": principal.application,
        "data_classification": "restricted",
        "metadata": {},
        "estimated_cost_usd": 0.0,
    })


app = FastAPI(
    title="Solstice AI Operations Control Plane",
    version="0.2.0",
    description="Telemetry, model economics, replay experiments and guardrails for enterprise AI.",
)
static = Path(__file__).resolve().parent / "static"


@app.middleware("http")
async def authenticate_product_api(request: Request, call_next):
    if request.url.path.startswith("/v1/"):
        if settings.secure_mode:
            if verifier is None:
                return JSONResponse(status_code=503, content={"detail": "Authentication not configured"})
            try:
                request.state.principal = verifier.verify(request.headers.get("authorization"))
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {},
                )
        elif settings.api_key:
            supplied = request.headers.get("x-aiops-key", "")
            if not secrets.compare_digest(supplied, settings.api_key):
                return JSONResponse(
                    status_code=401, content={"detail": "Missing or invalid X-AIOps-Key"}
                )
    return await call_next(request)


app.mount("/static", StaticFiles(directory=static), name="static")


@app.get("/")
def index():
    return FileResponse(static / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "backend": settings.storage if repo is None else repo.backend,
        "version": app.version,
        "pricing_version": economics.version,
        "providers": list(providers.providers),
    }


@app.post("/v1/demo/seed")
def demo_seed(request: Request, clear: bool = True):
    if settings.secure_mode or not settings.demo_mode:
        raise HTTPException(403, "Demo seeding is disabled outside local demo mode")
    s = services_for(request, "aiops:admin")
    return seed_demo(s.repo, economics, clear=clear)


@app.post("/v1/events")
def ingest(event: AIEvent, request: Request):
    s = services_for(request, "aiops:ingest")
    event = trusted_event(event, request)
    if settings.secure_mode or not event.estimated_cost_usd:
        event.estimated_cost_usd = economics.estimate_cost(
            event.model, event.input_tokens, event.output_tokens, event.cached_tokens
        )
    s.repo.insert_event(event.model_dump())
    return {
        "ok": True, "event_id": event.event_id,
        "estimated_cost_usd": event.estimated_cost_usd,
    }


@app.post("/v1/events/batch")
def ingest_batch(events: list[AIEvent], request: Request):
    s = services_for(request, "aiops:ingest")
    if len(events) > 5000:
        raise HTTPException(413, "Batch limit is 5,000 events")
    ids = []
    for event in events:
        event = trusted_event(event, request)
        if settings.secure_mode or not event.estimated_cost_usd:
            event.estimated_cost_usd = economics.estimate_cost(
                event.model, event.input_tokens, event.output_tokens, event.cached_tokens
            )
        s.repo.insert_event(event.model_dump())
        ids.append(event.event_id)
    return {"ok": True, "accepted": len(ids), "event_ids": ids}


@app.post("/v1/gateway/generate", response_model=GatewayResponse)
def generate(req: GatewayRequest, request: Request):
    s = services_for(request, "aiops:execute")
    if settings.secure_mode:
        principal: Principal = request.state.principal
        req = req.model_copy(update={
            "user_id": principal.subject,
            "team": principal.team,
            "application": principal.application,
            "agent": principal.application,
            "data_classification": "restricted",
            "metadata": {},
            "capture_for_replay": False,
        })
    estimated_input = max(1, int(len((req.prompt + " " + req.context).split()) * 1.32))
    ctx = req.model_dump() | {"estimated_input_tokens": estimated_input}
    p = s.policy_engine.evaluate(ctx)
    if p["decision"] == "deny":
        s.repo.audit("policy", authenticated_actor(request), req.workflow, "deny", {
            "reasons": p["reasons"],
            "provider": req.provider,
            "model": req.model,
            "data_classification": req.data_classification,
        })
        raise HTTPException(403, {
            "message": "Request denied by policy",
            "reasons": p["reasons"],
        })
    selected = p.get("route_model") or req.model
    try:
        adapter = providers.get(req.provider)
    except KeyError:
        raise HTTPException(400, "Provider is not configured")
    # Never replay or persist raw prompt/context automatically in secured mode.
    meta = dict(req.metadata)
    meta.setdefault("difficulty", {
        "low": .18, "medium": .45, "high": .72,
    }[req.task_complexity])
    result = adapter.generate(
        selected, req.prompt, req.context, req.max_output_tokens, meta
    )
    cost = economics.estimate_cost(
        selected, result.input_tokens, result.output_tokens, result.cached_tokens
    )
    expected = req.metadata.get("expected") or {}
    quality = None
    task_success = None
    if expected:
        quality, task_success = s.replay_engine.quality_score(
            result.output, result.output, expected
        )
    event = AIEvent(
        user_id=req.user_id, team=req.team, application=req.application,
        workflow=req.workflow, agent=req.agent, provider=req.provider, model=selected,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cached_tokens=result.cached_tokens, latency_ms=result.latency_ms,
        estimated_cost_usd=cost, success=result.success, error_code=result.error_code,
        tool_calls=req.tool_calls, retry_count=req.retry_count,
        data_classification=req.data_classification,
        policy_decision=p["decision"], policy_reasons=p["reasons"],
        task_success=task_success, quality_score=quality,
        metadata={} if settings.secure_mode else req.metadata,
    )
    s.repo.insert_event(event.model_dump())
    capture_id = None
    if req.capture_for_replay:
        wf = WorkflowCapture(
            name=req.workflow, prompt=req.prompt, context=req.context,
            baseline_provider=req.provider, baseline_model=selected,
            baseline_output=result.output,
            baseline_input_tokens=result.input_tokens,
            baseline_output_tokens=result.output_tokens,
            baseline_cost_usd=cost, expected=expected, metadata=meta,
        )
        s.repo.insert_workflow(wf.model_dump())
        capture_id = wf.workflow_id
    s.repo.audit("gateway", authenticated_actor(request), req.workflow, "allow", {
        "model_requested": req.model, "model_used": selected,
        "cost_usd": cost, "shadow_actions": p["shadow_actions"],
    })
    return GatewayResponse(
        request_id=event.request_id, trace_id=event.trace_id,
        provider=req.provider, model=selected, output=result.output,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cached_tokens=result.cached_tokens, latency_ms=result.latency_ms,
        estimated_cost_usd=cost, policy_decision=p["decision"],
        policy_reasons=p["reasons"], shadow_actions=p["shadow_actions"],
        workflow_capture_id=capture_id,
    )


@app.get("/v1/analytics/overview")
def overview(request: Request, hours: int = Query(24, ge=1, le=24*365)):
    return services_for(request).analytics.overview(hours)


@app.get("/v1/analytics/breakdown")
def breakdown(request: Request, group_by: str = "team", hours: int = 24, limit: int = 20):
    return services_for(request).analytics.group(group_by, hours, limit)


@app.get("/v1/events")
def list_events(request: Request, hours: int = 24, limit: int = 100):
    return services_for(request).repo.events(hours, limit)


@app.get("/v1/recommendations")
def recommendations(request: Request, hours: int = 24):
    s = services_for(request)
    return economics.recommendations(s.repo.events(hours, 5000), s.repo.replay_runs())


@app.get("/v1/workflows")
def workflows(request: Request, limit: int = 100):
    return services_for(request, "aiops:replay").repo.list_workflows(limit)


@app.get("/v1/workflows/{workflow_id}")
def workflow(workflow_id: str, request: Request):
    w = services_for(request, "aiops:replay").repo.get_workflow(workflow_id)
    if not w:
        raise HTTPException(404, "Workflow not found")
    return w


@app.post("/v1/workflows/{workflow_id}/replay")
def replay(workflow_id: str, req: ReplayCompareRequest, request: Request):
    s = services_for(request, "aiops:replay")
    if settings.secure_mode:
        # Production-safe redacted replay and destination authorization are
        # not implemented. Do not allow calling an alternate model with
        # potentially sensitive captures in secured mode.
        raise HTTPException(403, "Replay is disabled in secure mode pending data controls")
    w = s.repo.get_workflow(workflow_id)
    if not w:
        raise HTTPException(404, "Workflow not found")
    out = s.replay_engine.compare(
        w, req.candidate_models, req.provider, req.min_quality_score
    )
    s.repo.audit("replay", authenticated_actor(request), workflow_id, "completed", {
        "candidate_models": req.candidate_models,
        "recommendation": out.get("recommendation"),
    })
    return out


@app.get("/v1/replays")
def replays(request: Request, limit: int = 100):
    return services_for(request, "aiops:replay").repo.replay_runs(limit)


@app.post("/v1/policies")
def create_policy(p: PolicyCreate, request: Request):
    s = services_for(request, "aiops:admin")
    pid = s.repo.upsert_policy(p.model_dump())
    s.repo.audit("policy", authenticated_actor(request), pid, "upsert", {
        "name": p.name, "mode": p.mode, "priority": p.priority, "enabled": p.enabled,
    })
    return {"ok": True, "policy_id": pid}


@app.get("/v1/policies")
def policies(request: Request):
    return services_for(request).repo.list_policies()


@app.post("/v1/budgets")
def create_budget(b: BudgetCreate, request: Request):
    s = services_for(request, "aiops:admin")
    bid = s.repo.upsert_budget(b.model_dump())
    s.repo.audit("budget", authenticated_actor(request), bid, "upsert", {
        "scope_type": b.scope_type, "scope_value": b.scope_value,
        "monthly_limit_usd": b.monthly_limit_usd,
    })
    return {"ok": True, "budget_id": bid}


@app.get("/v1/budgets")
def budgets(request: Request):
    s = services_for(request)
    out = []
    for b in s.repo.list_budgets():
        spend = s.repo.monthly_spend(b["scope_type"], b["scope_value"])
        out.append({
            **b, "current_spend_usd": round(spend, 4),
            "utilization": round(spend / max(float(b["monthly_limit_usd"]), 1e-12), 4),
        })
    return out


@app.get("/v1/audit")
def audit(request: Request, limit: int = 100):
    return services_for(request, "aiops:audit").repo.audits(limit)
