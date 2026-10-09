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
    workflow: str = "unknown"
    agent: str = "none"
    provider: str = "unknown"
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    estimated_cost_usd: float = 0.0
    success: bool = True
    error_code: str = ""
    tool_calls: int = 0
    retry_count: int = 0
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
    workflow: str
    prompt: str
    context: str = ""
    provider: str = "mock"
    model: str = "standard-model"
    agent: str = "none"
    data_classification: str = "internal"
    max_output_tokens: int = 512
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
    monthly_limit_usd: float
    warning_threshold: float = 0.8
    hard_limit: bool = False
    overage_action: Literal["warn","downgrade","deny"] = "warn"

class ReplayCompareRequest(BaseModel):
    candidate_models: list[str] = Field(default_factory=lambda:["economy-model","standard-model","premium-model"])
    provider: str = "mock"
    min_quality_score: float = 0.90

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
