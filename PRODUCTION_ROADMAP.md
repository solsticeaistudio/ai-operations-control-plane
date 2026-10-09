# Production roadmap

## P0 — make telemetry authoritative

1. Enterprise SSO/workload identity at the gateway.
2. SDK and gateway integrations for the organization's top AI surfaces.
3. Provider contract-pricing registry with effective dates.
4. Data-classification metadata from the source system rather than user self-assertion.
5. Retention/redaction policy for prompt and replay data.

## P1 — make optimization trustworthy

1. Workflow-specific evaluators and labeled golden sets.
2. Replay sets, not single examples: sample representative executions by workflow, complexity and failure mode.
3. Statistical confidence intervals for projected savings and quality deltas.
4. Cost per successful business outcome as the primary unit metric.
5. Canary/shadow routing with automatic rollback thresholds.

## P2 — make governance enterprise-grade

1. Policy-as-code repository with review/approval and signed releases.
2. RBAC for policy authors, reviewers and operators.
3. Provider/model allowlists by team, data classification and geography.
4. Exception workflow with expiration and owner.
5. Incident export into SIEM/SOC tooling.

## P3 — wire full DeltaStore

The portable prototype stores immutable full workflow captures. Replace the replay persistence adapter with the existing Solstice DeltaStore implementation to gain parent-relative state deltas, branching histories, savepoints, deterministic state reconstruction and lower duplication for long-running workflow histories.

The product API does not need to change. The replay engine should request a materialized baseline state from DeltaStore, fork a counterfactual branch, run the alternate provider/configuration, and persist the resulting state delta plus evaluation.
