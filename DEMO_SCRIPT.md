# 4-minute product walkthrough

1. **Overview** — "The first problem is attribution. I want to know who is using AI, through what application and workflow, which model was called, the token/cost profile, and the outcome. ClickHouse is the telemetry system of record."
2. **Usage** — change the grouping from User → Team → Workflow. "This is the inventory layer David described: who is using AI and how."
3. **Optimization** — "Midas is looking for expensive behavior, not merely expensive vendors: premium-model overuse, low cache utilization, oversized context and retry waste."
4. **Replay Lab** — select a workflow and click Replay. "I do not want a cost recommendation to mutate production blindly. I replay the same captured workload against candidate models and compare cost, latency and outcome quality."
5. **Guardrails** — "Once an optimization is supported by evidence, it becomes a shadow policy. We observe what it *would* have done before enforcing it. Governance becomes a measured progression, not a blanket block list."

Close with: **"Midas tells me where to look. Replay tells me whether the cheaper or safer alternative actually works. Policy turns proven behavior into an organizational guardrail."**
