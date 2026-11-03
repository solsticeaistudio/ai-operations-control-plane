# Security model and production hardening

This repository is a product prototype, so it deliberately separates what is implemented from what an enterprise deployment still needs.

## Implemented controls

- Optional API-key protection for `/v1/*` endpoints (`AIOPS_API_KEY`, sent as `X-AIOps-Key`).
- Provider and data-classification guardrails.
- Context/token, retry and tool-call ceilings.
- Observe, shadow and enforce policy modes.
- Budget-triggered warn/downgrade/deny behavior.
- Central audit trail for gateway, replay and policy activity.
- Denied-request and policy-change audit records use selected operational fields instead of copying prompts, context, request metadata, or arbitrary policy rules/scopes.
- Model credentials remain in provider adapters/server-side configuration; model prompts do not receive provider keys.

## Important remaining data exposure risks

The audit fix does **not** make the entire prototype safe for sensitive production traffic:
- Workflow captures persist raw prompts and context and are accessible through workflow endpoints.
- Ingested AI events accept arbitrary client-supplied metadata.
- Some human-readable audit identifiers and reasons remain client-derived.
- Identity and classification attributes are currently self-asserted, and cross-tenant authorization is not implemented.

See [THREAT_MODEL.md](THREAT_MODEL.md) for trust boundaries and enterprise security gates.

## M2 secure API mode

Opt-in `AIOPS_SECURE_MODE=true` verifies OIDC bearer JWTs with the configured
HTTPS JWKS, issuer, audience and expiry, and binds each request to the verified
tenant's separate storage namespace. Per-route scopes limit ingest, read,
execution, replay inspection, audit inspection and administration. Secure mode
disables active replay and raw workflow capture, and blocks demo seeding.

See [IDENTITY_HARDENING.md](IDENTITY_HARDENING.md) for configuration, threat
boundaries and remaining limitations. Local demo mode is still unauthenticated
unless `AIOPS_API_KEY` is configured and must not be exposed publicly.

## Production identity design

Do **not** trust `user_id`, `team` or application identity supplied by arbitrary clients in production. The opt-in secure path now overrides them using broker-verified claims. Put the service behind the enterprise identity-aware gateway and derive identity from signed authentication context. Workload identity should be distinct from human identity. The event should carry both the initiating principal and executing workload.

Recommended shape:

```text
Human / workload
      │ enterprise SSO / workload identity
      ▼
Identity-aware gateway
      │ signed principal context
      ▼
AI Operations Gateway
      ├── policy decision
      ├── credential broker / provider adapter
      └── attributed telemetry
```

## Secrets

Use the company's secret manager or workload identity. Do not store provider keys in ClickHouse, workflow captures, telemetry metadata or replay payloads.

## Data minimization

The control plane does not need raw prompts for every telemetry event. Production deployments should make prompt/body capture opt-in by workflow and classification. Replay captures should support redaction, retention windows and encryption appropriate to the source data.

## Policy administration

The prototype stores policy versions but does not implement enterprise approvals or signatures. Production should add policy change approval, immutable version IDs, rollback, administrator RBAC, and separation of duties for high-impact enforcement.

## Replay safety

Counterfactual replay should default to frozen/mock tool responses. Replaying a trace must not repeat real-world writes (tickets, messages, payments, account changes) unless the operator explicitly chooses a sandboxed integration.
