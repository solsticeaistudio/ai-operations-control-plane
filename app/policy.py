from __future__ import annotations
from typing import Any

class PolicyEngine:
    def __init__(self, repo, economics): self.repo=repo; self.economics=economics

    @staticmethod
    def _scope_matches(scope: dict[str,Any], ctx: dict[str,Any]) -> bool:
        for key in ("teams","applications","workflows","users"):
            values=scope.get(key)
            field={"teams":"team","applications":"application","workflows":"workflow","users":"user_id"}[key]
            if values and ctx.get(field) not in values: return False
        return True

    def evaluate(self, ctx: dict[str,Any]) -> dict[str,Any]:
        policies=[p for p in self.repo.list_policies() if p.get("enabled",True)]
        policies.sort(key=lambda p:int(p.get("priority",100)))
        effective="allow"; reasons=[]; shadow_actions=[]; route_model=None
        for p in policies:
            if not self._scope_matches(p.get("scope") or {},ctx): continue
            rules=p.get("rules") or {}; violations=[]; actions=[]
            if rules.get("allowed_providers") and ctx.get("provider") not in rules["allowed_providers"]: violations.append(f"provider {ctx.get('provider')} is not approved")
            if ctx.get("provider") in (rules.get("blocked_providers") or []): violations.append(f"provider {ctx.get('provider')} is blocked")
            if rules.get("max_input_tokens") is not None and int(ctx.get("estimated_input_tokens",0))>int(rules["max_input_tokens"]): violations.append(f"estimated input exceeds {rules['max_input_tokens']:,} token limit")
            if rules.get("max_retries") is not None and int(ctx.get("retry_count",0))>int(rules["max_retries"]): violations.append(f"retry count exceeds {rules['max_retries']}")
            if rules.get("max_tool_calls") is not None and int(ctx.get("tool_calls",0))>int(rules["max_tool_calls"]): violations.append(f"tool-call count exceeds {rules['max_tool_calls']}")
            restricted=rules.get("restricted_data_providers") or {}
            classification=ctx.get("data_classification")
            allowed_for_class=restricted.get(classification)
            if allowed_for_class and ctx.get("provider") not in allowed_for_class: violations.append(f"{classification} data may only use providers: {', '.join(allowed_for_class)}")
            if violations:
                if p.get("mode")=="enforce": effective="deny"
                elif p.get("mode")=="shadow": shadow_actions.extend([f"{p['name']}: would deny — {v}" for v in violations])
                reasons.extend([f"{p['name']}: {v}" for v in violations])
            if rules.get("route_low_complexity_to") and ctx.get("task_complexity")=="low":
                candidate=rules["route_low_complexity_to"]
                if p.get("mode")=="enforce": route_model=candidate
                else: shadow_actions.append(f"{p['name']}: would route low-complexity task to {candidate}")

        # Budget layer
        for b in self.repo.list_budgets():
            field={"team":"team","application":"application","workflow":"workflow"}[b["scope_type"]]
            if ctx.get(field)!=b["scope_value"]: continue
            spend=self.repo.monthly_spend(b["scope_type"],b["scope_value"])
            limit=float(b["monthly_limit_usd"]); util=spend/max(limit,1e-12)
            if util>=float(b.get("warning_threshold",.8)):
                reasons.append(f"budget {b['scope_type']}={b['scope_value']} is {util:.0%} utilized")
            if util>=1.0:
                action=b.get("overage_action","warn")
                if action=="deny" and b.get("hard_limit"): effective="deny"
                elif action=="downgrade": route_model="economy-model"; shadow_actions.append("budget policy routes overage traffic to economy tier")
        return {"decision":effective,"reasons":reasons,"shadow_actions":shadow_actions,"route_model":route_model}
