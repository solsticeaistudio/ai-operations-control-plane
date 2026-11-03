# AI Operations Control Plane — threat model

**Status:** prototype threat model, updated 2026-11-03. This document describes *current behavior* separately from *production requirements*. It is not a security certification.

## Purpose and assets

The control plane attributes AI activity to people, apps, agents, and workflows; stores telemetry; estimates costs; supports replay; and evaluates observe/shadow/enforce policies. Assets include customer/employee identity, AI usage events, provider credentials, prompts and replay captures, policy definitions, budget rules, and audit records.

## Data flow / trust boundaries

```text
[Untrusted client / SDK / agent]
          |  signed OIDC tenant/principal/scopes in secure mode; workflow label still client supplied
          v
[FastAPI gateway / ingestion] -----> [Provider adapter / external model]
          |       |
          |       +-- evaluate policies before gateway execution
          v
[Telemetry, policy, workflow and audit repository]
       |                                |
       +--> [Economics / analytics]     +--> [Replay engine]
                                         |
                                         +--> [Provider adapter]
```

| Trust boundary | Prototype state | Production requirement |
| --- | --- | --- |
| Client to API | Local demo can use optional API key; opt-in secure mode verifies signed OIDC JWTs and scope at each `/v1/*` route | Real IdP federation, token lifecycle and abuse rate limits |
| Identity / tenant | Secure mode derives subject/team/app from signed claims and uses tenant-specific database namespaces | Trusted upstream membership mapping, dedicated DB credentials, quotas, operational isolation |
| Gateway to model | Model credentials are held in server-side adapter configuration | Secrets manager, credential rotation, endpoint allowlists, outbound restrictions, egress monitoring |
| API to repository | Secure mode uses distinct hashed SQLite files or ClickHouse databases; secure ClickHouse requires HTTPS and never silently falls back | Private networking, tenant-appropriate DB accounts, encryption at rest, backups |
| Analytics / audit | Scope-authorized access uses only the verified tenant's repository; separate `aiops:audit` permission | Full role lifecycle, access logging, retention, integrity and quota protections |
| Replay to providers | In local demo captures can include full prompts; secure mode disables capture and active replay | Secure redacted capture storage, approved replay destinations, side-effect boundaries |

## Threats and decisions

### T1. Sensitive information leaks into audit logs

**Attack/failure:** A denied request contains a customer secret in its prompt, context or arbitrary metadata. Copying the entire request into an audit record unnecessarily preserves the secret.

**Implemented mitigation:** The denied-request audit now records only a restricted set of operational fields (reason text, provider, model, classification), not the original request dictionary. Policy-change audits no longer duplicate arbitrary policy rules and scopes. Tests use unique sensitive-data canaries and assert their absence from the audit records.

**Residual risk:** Reason text and some labels may still contain client-controlled text. Secure mode strips arbitrary ingested metadata and disables gateway replay captures, but telemetry body classification, retention, integrity and audit scrubbing are not production-grade. Local demo mode still accepts arbitrary metadata and can store raw prompts. Do not send sensitive production workloads as-is.

### T2. Spoofed identities and cross-tenant data exposure

**Attack/failure:** A client claims another person's `user_id` or tenant, or a read query fails to filter by authenticated tenant. Current audit attribution can be spoofed.

**Production mitigation:** Enforce SSO with customer identity federation and workload credentials. Validate JWT signature, issuer, audience and expiry at an identity-aware gateway; derive tenant and principal server-side. Require tenant scoping in every table/key and every read/write path, with explicit negative cross-tenant tests. Separate human principal from executing agent identity and limit delegated permissions.

**Status:** Partially implemented in opt-in secure mode: signed OIDC JWT verification, per-route scope authorization, verified attribution and separate tenant storage namespaces with negative access tests. Enterprise IdP federation, membership mapping, tenant-specific database credentials, revocation and service quotas remain unimplemented. The local demo still trusts caller fields.

### T3. Stolen provider credentials or network interception

**Attack/failure:** A provider key appears in logs, clients gain direct access to shared secrets, or an internal/database connection uses insecure transport.

**Implemented mitigation:** Provider adapters keep keys in server configuration rather than model prompts.

**Production mitigation:** Dedicated secret manager, short-lived workload identity where supported, least-privilege access, TLS in transit, private connectivity/allowlisting for sensitive services, encryption at rest and key rotation.

**Status:** Secure mode requires HTTPS ClickHouse and approved external model endpoints; provider keys remain server-side. Enterprise secret lifecycle and private networking are not supplied by this repository.

### T4. Unauthorized policy administration or unsafe rollout

**Attack/failure:** An attacker creates a permissive policy, bypasses budget limits, or turns a shadow policy into live enforcement without review.

**Implemented mitigation:** Policy matching and observe/shadow/enforce behavior exist. Gateway enforcement can deny before model execution. Audit records identify policy changes.

**Production mitigation:** Separate policy author/reviewer/operator RBAC, signed/versioned releases, approvals, rollback, expiry for exceptions and a default-deny policy for unapproved sensitive actions.

**Status:** Secure mode enforces an `aiops:admin` API scope for policy/budget mutations. Fine-grained administrative RBAC, two-person approvals and policy signing remain unimplemented.

### T5. Replay creates data disclosure or unintended side effects

**Attack/failure:** Stored prompts expose data to an alternative provider, or a replayed tool action sends a message, modifies a ticket or processes a payment twice.

**Implemented behavior:** Replay re-executes a captured text workload through a provider adapter; this prototype's deterministic mock provider can be used offline. The prototype does not implement arbitrary tool-action replay.

**Production mitigation:** Data-classification checks on replay destination, approved provider and residency constraints, redacted captures, explicit opt-in for content retention, frozen/mock tool outputs by default, and sandbox/approval barriers for any side-effecting integration.

**Status:** Secure mode disables active replay and gateway capture; local demo remains illustrative. The local quality scorer is not a validated production benchmark.

See [IDENTITY_HARDENING.md](IDENTITY_HARDENING.md) for secure mode setup and limitations.

## Minimum security gates for an enterprise pilot

- Mandatory authentication for API reads/writes; validate issuer/audience/expiry, derive a tenant/principal from trusted identity.
- Reject cross-tenant event, policy, workflow, budget and audit access (including admin paths); test negative cases.
- Scrub secrets/PII from operational logs; configure body capture off by default and apply classification-aware retention.
- Protect provider and database credentials using company secret management; require TLS and private networking where appropriate.
- Require controlled administrative policy changes and a fast, tested rollback.
- Verify replay has no uncontrolled side effects; run representative quality and security evaluations before enforcement.

## Design review questions

1. **Why observe before enforce?** It lets teams quantify impacts and false positives before modifying real traffic.
2. **Why keep policy enforcement outside the model?** An LLM response is untrusted; authorization must occur in a separate deterministic control.
3. **What would you do first for production?** Trusted identity and tenant isolation, then data minimization, secure connectivity, controlled rollout and auditable operations.
4. **What remains a prototype?** Customer IdP federation, tenant membership mapping, DB-level least privilege, secret lifecycle, safe captures and production evaluation.
