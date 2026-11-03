# Interview walkthrough — AI Operations Control Plane

**Audience:** technical manager / Product Security. **Time:** about 4 minutes, with follow-up discussion. Use these notes to practice; don't recite them word-for-word.

## One-minute explanation

> I built an AI Operations Control Plane prototype for a problem I see emerging as companies adopt more AI: they can't easily attribute activity and spend to the right teams, or know which optimizations are safe.
>
> The system accepts AI telemetry either through an SDK or through a policy-aware gateway. It organizes usage by user, team, application, workflow, provider and model. ClickHouse is the intended telemetry backend, with SQLite so the prototype runs locally.
>
> Midas examines spending patterns, and replay lets me compare captured workloads against candidate models on cost, latency and a lightweight quality measure. Policies then move through observe, shadow and enforce rather than changing production behavior blindly.
>
> It's an implemented prototype, not an enterprise-ready control plane. I've made the audit trail more selective about logging sensitive request details, and I'd prioritize trusted identity, tenant isolation and safe capture retention before production use.

## Walkthrough route

1. **Dashboard / attribution** — show one workflow's events and cost; explain the time/team/application/workflow dimensions in ClickHouse. Note that local synthetic demo traffic is not real organizational data.
2. **Gateway and policy** — point to `app/main.py` and `app/policy.py`. Requests pass policy evaluation before an adapter is invoked; observe/shadow record would-be actions, while enforce can deny.
3. **Economics** — open `app/economics.py`. Explain cached versus uncached tokens and why cost per successful task beats raw per-token price.
4. **Replay** — open `app/replay.py`. Captured workload can be evaluated against mock or compatible model adapters; current evaluator is intentionally basic and needs task-specific labels before production.
5. **Security** — explain the change to denial audits: no longer copy prompts, context, or arbitrary metadata into audit records; regression tests check canaries. Explain remaining risks without overstating.

## Component map

```text
Applications / Agents
     |
     +--> SDK telemetry --------------------+
     |                                     |
     +--> Gateway -> policy -> adapter     |
               |            |               |
               +-- audit    +-- model      |
               |                            |
               +--> usage events -----------+
                                             v
                                  ClickHouse / SQLite
                                     /          \
                                 Economics      Replay
                                     \          /
                                     Recommendations
                                            |
                                    Shadow -> Enforce
```

## Security follow-ups (answer with current state, then next step)

**Q: What stops one tenant reading another's events?** Not yet guaranteed. The prototype has no tenant-aware authorization. I'd integrate enterprise identity, bind tenant identity to trusted authentication context, enforce tenant-scoped access at every storage/API boundary, and add cross-tenant negative tests.

**Q: Where are keys stored?** The provider adapter loads server-side configuration rather than inserting secrets into prompts. Enterprise rollout needs a managed secret store, rotation, workload identity, and outbound connection controls.

**Q: What about dangerous data in logs?** Denied request audits now capture limited operational metadata, and policy-change audits avoid duplicating rules/scopes. Other paths can still retain raw workflow captures or arbitrary telemetry metadata; production requires minimization, redaction, classification-based capture approval and retention.

**Q: Does shadow mode prevent attacks?** No. Shadow reports what a policy *would* have done. Only enforce mode actually denies. Identity and tenant checks must be mandatory outside discretionary policy modes.

**Q: Can replay accidentally trigger real actions?** The current prototype replays text generation through model adapters; it doesn't replay external tools. If tool replay is added, default to frozen tool responses, sandbox connections and explicit approvals before writes.

**Q: Why ClickHouse instead of SQLite?** The intended shape is high-volume, time-oriented AI telemetry with analytics across many dimensions; SQLite makes the developer demo portable.

**Q: What if you need to scale it across clouds?** Keep provider and storage adapters separate, use an identity-aware ingress, defined tenant boundaries, central policy semantics, and cloud-neutral audit events. Deployment specifics follow actual scale and regional requirements.

## Useful clarifying questions for Michael

- Is this a single-tenant service or a shared multi-tenant control plane?
- Are requests read-only, or can they initiate writes/tool actions?
- Who issues identity assertions, and where is authorization enforced?
- Are customers using private connectivity or public endpoints?
- What are the data residency and retention constraints?

## Three things not to claim

- Don't say enterprise SSO, verified user identity, per-tenant RBAC or encryption/key management are already implemented.
- Don't describe the local replay scoring as a validated production eval suite.
- Don't claim the audit fix makes all telemetry or workflow capture storage PII-safe.

**Closing:** "The central design choice is to separate visibility, evidence, and enforcement. I want the organization to see what AI is doing, prove which changes are safe, and govern execution with controls outside the model."
