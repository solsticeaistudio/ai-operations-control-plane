from __future__ import annotations
from collections import defaultdict, Counter
from typing import Any

class AnalyticsService:
    def __init__(self,repo,economics): self.repo=repo; self.economics=economics
    def overview(self,hours=24):
        rows=self.repo.events(hours=hours)
        users={r["user_id"] for r in rows}; teams={r["team"] for r in rows}; spend=sum(float(r["estimated_cost_usd"] or 0) for r in rows); success=[r for r in rows if r.get("task_success") is not None]
        successful=sum(1 for r in success if r.get("task_success")); cost_per_success=spend/max(successful,1) if successful else None
        policy=sum(1 for r in rows if r.get("policy_decision") not in ("allow","observe") or r.get("policy_reasons"))
        return {"hours":hours,"backend":self.repo.backend,"calls":len(rows),"active_users":len(users),"teams":len(teams),"spend_usd":round(spend,4),"input_tokens":sum(int(r["input_tokens"] or 0) for r in rows),"output_tokens":sum(int(r["output_tokens"] or 0) for r in rows),"cached_tokens":sum(int(r["cached_tokens"] or 0) for r in rows),"avg_latency_ms":round(sum(int(r["latency_ms"] or 0) for r in rows)/max(len(rows),1)),"task_success_rate":round(successful/max(len(success),1),4) if success else None,"cost_per_successful_task_usd":round(cost_per_success,6) if cost_per_success is not None else None,"policy_events":policy}
    def group(self,group_by="team",hours=24,limit=20):
        field=group_by if group_by in {"team","application","workflow","provider","model","user_id"} else "team"; buckets=defaultdict(lambda:{"calls":0,"spend_usd":0.0,"input_tokens":0,"output_tokens":0,"cached_tokens":0,"successes":0,"scored":0})
        for r in self.repo.events(hours=hours):
            b=buckets[r.get(field,"unknown")]; b["calls"]+=1; b["spend_usd"]+=float(r.get("estimated_cost_usd",0)); b["input_tokens"]+=int(r.get("input_tokens",0)); b["output_tokens"]+=int(r.get("output_tokens",0)); b["cached_tokens"]+=int(r.get("cached_tokens",0));
            if r.get("task_success") is not None: b["scored"]+=1; b["successes"]+=int(bool(r["task_success"]))
        out=[]
        for name,b in buckets.items():
            out.append({"name":name,**{k:(round(v,4) if isinstance(v,float) else v) for k,v in b.items()},"success_rate":round(b["successes"]/max(b["scored"],1),4) if b["scored"] else None,"cache_ratio":round(b["cached_tokens"]/max(b["input_tokens"],1),4)})
        return sorted(out,key=lambda x:x["spend_usd"],reverse=True)[:limit]
