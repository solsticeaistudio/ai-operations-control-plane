# Architecture

## Product thesis

The control plane turns raw AI usage into evidence-backed governance:

**Observe → Attribute → Analyze → Replay → Shadow → Enforce**

```text
Apps / copilots / agents
        │
        ▼
 AI Gateway / SDK ───────────────► model providers
        │
        ├── preflight policy evaluation
        └── attributed telemetry
                    │
                    ▼
              ClickHouse
          (SQLite local fallback)
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
   Midas economics       Replay engine
 cost / budgets /       counterfactual runs
 opportunities          same captured workload
          └─────────┬─────────┘
                    ▼
             Guardrail policy
        observe / shadow / enforce
```

## ClickHouse role

ClickHouse is the intended telemetry system of record. The schema is optimized around time, team, application and workflow dimensions so high-cardinality AI events can be sliced by the organizational questions that matter: who is using AI, through which surface, which models, at what cost, and with what operational/policy outcome.

The repository boundary has a SQLite implementation only so the product can run immediately on a laptop. `docker compose up --build` uses ClickHouse.

## Midas

Midas is the economics engine. The production-facing implementation here keeps pricing as configuration, distinguishes cached from uncached input, calculates per-event cost, surfaces budget posture, and generates workflow-level optimization opportunities. Recommendations remain evidence, not authority.

## Replay / Delta

The replay layer uses immutable baseline workflow captures and creates counterfactual branches against alternate model configurations. The current prototype persists captures directly for portability. The interface is intentionally compatible with swapping in the existing Solstice DeltaStore lineage engine, whose core semantics are parent-relative deltas, reconstruction, branching and replay.

A replay is useful only when it carries an evaluation. The prototype compares task success, output quality, latency and cost. The headline metric is **cost per successful task**, not token price in isolation.

## Guardrails

Policies support observe, shadow and enforce modes. This allows discovery first, then measured rollout. The policy engine currently supports provider allowlists, classification/provider restrictions, context limits, retry/tool-call limits, model routing for low-complexity work, and budget-triggered actions.

## Model agnosticism

Provider adapters isolate the control plane from a single model vendor. `MockProvider` enables deterministic local evaluation. An OpenAI-compatible adapter is included for real endpoints. Additional provider adapters can be added without changing telemetry, economics, replay or policy logic.
