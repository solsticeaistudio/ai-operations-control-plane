from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field
import uuid

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class AIEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    timestamp: datetime = Field(default_factory=utcnow)
    request_id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    trace_id: str = Field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    session_id: str | None = None
    user_id: str = "unknown"
    team: str = "unknown"
    application: str = "unknown"
    workflow: str = Field(default="unknown", max_length=128)
    agent: str = "none"
    provider: str = Field(default="unknown", max_length=128)
    model: str = Field(default="unknown", max_length=128)
    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cached_tokens: int = Field(default=0, ge=0, le=10_000_000)
    latency_ms: int = Field(default=0, ge=0, le=86_400_000)
    estimated_cost_usd: float = Field(default=0.0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    success: bool = True
    error_code: str = ""
    tool_calls: int = Field(default=0, ge=0, le=1_000_000)
    retry_count: int = Field(default=0, ge=0, le=1_000_000)
    data_classification: str = "internal"
    policy_decision: str = "allow"
    policy_reasons: list[str] = Field(default_factory=list)
    task_success: bool | None = None
    quality_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class GatewayRequest(BaseModel):
    user_id: str
    team: str
    application: str
    workflow: str = Field(max_length=128)
    prompt: str = Field(min_length=1, max_length=32768)
    context: str = Field(default="", max_length=32768)
    provider: str = Field(default="mock", max_length=128)
    model: str = Field(default="standard-model", max_length=128)
    agent: str = "none"
    data_classification: str = "internal"
    max_output_tokens: int = Field(default=512, ge=1, le=8192)
    tool_calls: int = 0
    retry_count: int = 0
    task_complexity: Literal["low","medium","high"] = "medium"
    metadata: dict[str, Any] = Field(default_factory=dict)
    capture_for_replay: bool = True

class GatewayResponse(BaseModel):
    request_id: str
    trace_id: str
    provider: str
    model: str
    output: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    estimated_cost_usd: float
    policy_decision: str
    policy_reasons: list[str]
    shadow_actions: list[str] = Field(default_factory=list)
    workflow_capture_id: str | None = None

class PolicyCreate(BaseModel):
    name: str
    mode: Literal["observe","shadow","enforce"] = "shadow"
    priority: int = 100
    enabled: bool = True
    scope: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, Any] = Field(default_factory=dict)

class BudgetCreate(BaseModel):
    scope_type: Literal["team","application","workflow"]
    scope_value: str
    monthly_limit_usd: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    warning_threshold: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False)
    hard_limit: bool = False
    overage_action: Literal["warn","downgrade","deny"] = "warn"

class ReplayCompareRequest(BaseModel):
    candidate_models: list[str] = Field(default_factory=lambda:["economy-model","standard-model","premium-model"])
    provider: str = "mock"
    min_quality_score: float = Field(default=0.90, ge=0, le=1, allow_inf_nan=False)

class WorkflowCapture(BaseModel):
    workflow_id: str = Field(default_factory=lambda: f"wf_{uuid.uuid4().hex[:16]}")
    captured_at: datetime = Field(default_factory=utcnow)
    name: str
    prompt: str
    context: str = ""
    baseline_provider: str
    baseline_model: str
    baseline_output: str
    baseline_input_tokens: int
    baseline_output_tokens: int
    baseline_cost_usd: float
    expected: dict[str, Any] = Field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
