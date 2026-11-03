# M3 — API Abuse Controls, Audit Minimization and Local-Only Demo

**Status:** Opt-in secure API prototype. This is defense in depth, **not** a production security certification.

## Why this milestone

M2 established a signed-OIDC identity boundary, scope checks, and separate tenant
storage namespaces. M3 limits what a compromised but authenticated client can do
to the API and reduces sensitive text inadvertently written into audit records.

## Implemented protections

| Threat | M3 control | Limit |
| --- | --- | --- |
| Unbounded requests / expensive parsing | Streaming-aware POST/PUT/PATCH body cap, default 256 KiB | Per-request cap, not global bandwidth control |
| Repeated requests / credential stuffing | Process-local rate limits by connection source and signed tenant/subject; HTTP 429 with Retry-After | Not shared across workers or nodes; ingress quotas still needed |
| Large query/export results | Positive bounded pagination and lookback inputs | Some analytical endpoints still process up to their existing fixed fetch ceilings |
| Fake policy outcome in telemetry | In secure mode client-supplied policy verdict, reason text, session ID and error detail are not authoritative | Ingest tokens, workflow labels and success signals remain user-reported |
| Audit-log sensitive data | Secure-mode gateway audits hash workflow labels and exclude user-controlled model strings, reasons, shadow text and policy/budget labels | Hashes are identifiers, not encryption. Other telemetry still requires schema/retention review |
| Exposed local demo services | Docker Compose binds API and ClickHouse ports to 127.0.0.1 | Local process startup and custom deployments still require network hardening |
| Container privilege | API Docker image runs as UID 10001 | Still needs image scanning, runtime policies and read-only filesystem design |
| Development UI in secure mode | Static dashboard, Swagger/ReDoc and schema endpoints return 404; health is minimal | Backend API needs a real authenticated frontend |

Secure API responses include `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`.

## Configuration

M3 only affects `AIOPS_SECURE_MODE=true`, except for shared input-schema
validation and the safer local Docker configuration. Existing local demo
behavior still works, and the demo is still **not for Internet exposure**.

```sh
AIOPS_SECURE_BODY_MAX_BYTES=262144
AIOPS_SECURE_RATE_PER_MINUTE=300
AIOPS_SECURE_IP_RATE_PER_MINUTE=600
```

Limits are **per process**. For a multi-replica service, use a shared
Redis/API-gateway quota mechanism, request and token budgets by tenant,
and ingress/WAF abuse controls. Connection-source limits use the actual
ASGI socket address, not attacker-controlled `X-Forwarded-For`; when deployed
behind a reverse proxy, many clients may share that address and should be
handled primarily by a trusted edge limiter.

## Data and audit distinctions

- Verified broker `sub` and tenant claims determine identity/storage.
- Ingested `policy_decision` is marked `unverified` rather than pretending
  the control plane made a decision. Policy counters exclude unverified claims.
- Actual gateway `policy_decision` still comes from the server-side policy
  engine, not the caller.
- The secure audit includes a short digest of an untrusted workflow label,
  an action and safe operational counts, not raw prompts, model names, policy
  labels, scopes, or reasons.
- This does **not** mean that all events are scrubbed. Per-tenant telemetry
  still contains workflow and model labels and claimed token counts. A malicious
  trusted workload can fabricate usage events. Authentic telemetry will require
  source attestation and trusted instrumentation.
- Hashing workflow names reduces accidental plaintext retention but does not
  provide secrecy for predictable names; apply retention and access controls.

## Validation

GitHub Actions executes pytest, including regressions for malformed tokens,
tenant access, authorization, oversized content-length and chunked bodies,
request rate limits, query caps, malformed numeric inputs, audit canaries,
local-only demo ports and non-root image configuration.

## Secure development and SQL query hardening (M3.1)

- GitHub Actions runs `pip-audit` on the explicitly pinned direct Python
  dependencies and Bandit on the application, failing the PR on medium/high
  findings. Treat this as an automated baseline, not a complete supply-chain
  audit of all transitive dependencies or a substitute for code review.
- ClickHouse queries that consume workflow IDs, time windows, limits, and
  billing scopes use **typed query placeholders** with separate
  `param_*` HTTP arguments instead of manual quote escaping.
- SQLite identifiers are selected only from static known query strings,
  while values use DB-API bound parameters.
- SQL tests include an injection-shaped workflow identifier and assert it
  does **not** appear inside the SQL string sent to ClickHouse.
- A deployed ClickHouse integration test and a database account with restricted
  DDL/query privileges remain production requirements.

## Next enterprise requirements

1. Real customer federation, tenant onboarding and revocation.
2. Distributed ingress/tenant quotas, hardened API gateway and trusted proxy configuration.
3. Source-attested telemetry, schema-driven PII minimization and retention/DSR.
4. Immutable tamper-evident audit ledger and administration approval workflows.
5. Dedicated per-tenant DB credentials and deployed ClickHouse/transport integration tests.
6. Signed policy changes, rollback, disaster recovery, secrets manager and key rotation.
7. Authenticated browser UI and private networking for production deployments.

**Security design point:** authenticating a user does not make *all content they
send* trustworthy. Keep authorization separate from model outputs, claims
separate from verified facts, and data capture separate from audit evidence.
