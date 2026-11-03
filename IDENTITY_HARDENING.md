# M2: Identity and tenant-bound authorization

**Status:** implemented prototype security boundary, not an enterprise-certified deployment.

## Deployment modes

The original local demo remains available with `AIOPS_DEMO_MODE=true` and `AIOPS_SECURE_MODE=false`. It is **not suitable for Internet exposure**.

To enable authenticated API mode, configure environment variables before starting FastAPI:

```sh
AIOPS_DEMO_MODE=false
AIOPS_SECURE_MODE=true
AIOPS_OIDC_ISSUER=https://identity.example.com/
AIOPS_OIDC_AUDIENCE=aiops-control-plane
AIOPS_OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
AIOPS_SECURE_ALLOWED_PROVIDERS=mock
AIOPS_STORAGE=sqlite
AIOPS_SQLITE_PATH=./data/secure-aiops.db
```

*Values shown are placeholders, not a real identity provider.* Secure mode fails at service startup when issuer, audience or HTTPS JWKS URL is missing. Setting `AIOPS_DEMO_MODE=false` without secure mode is also refused. A legacy `X-AIOps-Key` is not a substitute for OIDC in secure mode.

For ClickHouse-backed secured storage, configure `AIOPS_STORAGE=clickhouse` and a **TLS `https://`** `CLICKHOUSE_URL`, plus authorized database credentials. The system refuses to fall back silently to SQLite when a secured ClickHouse connection fails.

## Authentication

All `/v1/*` routes require `Authorization: Bearer <JWT>`. Verify tokens using PyJWT 2.15.1 (RSA signature via trusted JWKS); validate **issuer, audience, expiry and subject**. Signing key selection happens only from the administrator-configured JWKS URL, never a token-controlled URL or algorithm.

Tokens must be issued by a trusted enterprise identity broker with these signed claims:

- `sub`: authenticated user or workload identity (required)
- `tenant_id`: trusted tenant identifier (required)
- `scope`: space-delimited API scopes (required to perform actions)
- `team` and `application`: optional authenticated attribution claims

**Critical assumption:** Customer identity federation and the issuer-to-tenant membership mapping happen **upstream** at the trusted broker. Do not allow clients to request arbitrary `tenant_id` claims from the broker. This app validates the trusted broker's signature; it does not independently provision tenants, validate memberships against an HR directory, or implement customer IdP federation.

| Scope | Authorizes |
| --- | --- |
| `aiops:read` | Events, analytics, recommendations, policies and budgets |
| `aiops:ingest` | Emit AI telemetry |
| `aiops:execute` | Execute through the model gateway |
| `aiops:replay` | Inspect captured workflows and replay results (active replay disabled in secure mode) |
| `aiops:audit` | Read tenant audit records |
| `aiops:admin` | All above plus policy/budget administration |

Authorization is enforced at each route after token verification. Read-only access does not grant policy mutation or audit access.

## Tenant data boundary

Authenticated tenant identity selects a **separate physical backend namespace**:

- SQLite: distinct hashed database filename under configured path
- ClickHouse: distinct hashed database name created from configured prefix

A token issued for tenant B cannot query tenant A's repository through the API, even with A's event IDs or workflow IDs. Hashing tenant identifiers avoids path traversal and direct name collisions. Current SQLite and ClickHouse implementations still use the same **server process and database credentials across tenants**. For stronger isolation, deploy dedicated DB credentials/roles, resource quotas, backups and operational controls per tenant. The current design is a prototype isolation boundary, not proof of complete infrastructure isolation.

## Trusted attribution and safe defaults

- A verified `sub`, `team`, and `application` override client-asserted user/team/app/agent fields in ingestion and gateway requests.
- Secure telemetry ingestion generates its own event/request/trace IDs and timestamps, discards arbitrary metadata, resets estimated cost and computes server-side cost using configured model prices.
- The unverified client data-classification label is replaced with `restricted` until the system can obtain classification from a trusted source.
- Workflow strings remain untrusted client labels. Secure mode rejects workflow-scoped policies and budgets to avoid using these strings as authorization principals.
- Gateway provider execution uses an explicit allowlist, **defaulting only to the deterministic mock adapter**. An allowed OpenAI-compatible remote endpoint must use HTTPS. Endpoint approval and data classification still require a full enterprise policy review.
- Workflow capture is disabled in secure mode by default even if callers request it. Active replay is disabled pending safe capture handling and destination checks.
- Demo data seeding is unavailable in secure mode, removing the database-wiping demo endpoint.
- Operational audit records do not duplicate raw prompts, contexts or arbitrary policy rule bodies.

## Security tests

`tests/test_identity_security.py` covers:

- Missing, expired, wrong-audience and wrong-issuer JWTs, invalid tenant identifiers, forged signatures
- Read-only principals denied execution, admin mutation and audit inspection
- Tenant A events, analytics, policies, budgets, workflows and audits invisible to tenant B
- Identity spoofing through user-supplied event fields; untrusted metadata removed
- Gateway request captures and remote providers denied under secure defaults
- Demo-seed disabled

Run `python -m pytest -q` or inspect the GitHub Actions PR check.

## Remaining production work

1. **Enterprise federation and membership:** configure the actual OIDC identity broker, issuer/tenant mapping, app enrollment, token lifecycle, workload service identities and revocation strategy.
2. **Storage isolation:** database-level least-privilege credentials, resilient migrations, connection pooling, quotas and per-tenant encryption/backups; verify ClickHouse mode in a deployed environment.
3. **Sensitive telemetry and replay:** structured allowlisted telemetry schemas, classification-aware retention/redaction, secure capture storage and approved replay destinations.
4. **Data integrity:** verified workflow identities and signed instrumentation events (currently a token with `aiops:ingest` can still falsify token counts and workflow labels).
5. **Frontend:** the static local dashboard does not perform an enterprise OIDC login flow or send bearer tokens. Secure mode is API-oriented until a real authenticated UI is built.
6. **Operational defense:** rate limiting, abuse quotas, monitoring, role administration, rollout controls, audit integrity and secret rotation.

Do not claim this implements end-to-end enterprise SSO or production-ready multitenant security. It implements a small, testable trust boundary and provides concrete follow-up design decisions.
