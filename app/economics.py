from __future__ import annotations
import json, math
from pathlib import Path
from collections import defaultdict, Counter
from typing import Any

class MidasEconomics:
    """Model economics engine derived from the user's Midas design.

    It is intentionally provider-agnostic: pricing is configuration, not logic.
    """
    def __init__(self, pricing_file: str):
        payload=json.loads(Path(pricing_file).read_text(encoding="utf-8"))
        self.version=payload.get("version","unknown")
        self.note=payload.get("note","")
        self.models=payload.get("models",{})

    def price(self, model: str) -> dict[str, Any]:
        return self.models.get(model) or self.models.get("standard-model") or {
            "input_per_million": 1.0, "cached_input_per_million": .25,
            "output_per_million": 4.0, "tier":"standard", "provider":"unknown"
        }

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int=0) -> float:
        p=self.price(model)
        cached=max(0,min(int(cached_tokens or 0), int(input_tokens or 0)))
        uncached=max(0,int(input_tokens or 0)-cached)
        cost=(uncached/1_000_000)*float(p["input_per_million"])
        cost+=(cached/1_000_000)*float(p.get("cached_input_per_million",p["input_per_million"]))
        cost+=(int(output_tokens or 0)/1_000_000)*float(p["output_per_million"])
        return round(cost,8)

    def estimate_candidate(self, candidate_model: str, input_tokens: int, output_tokens: int, cached_tokens: int=0) -> float:
        return self.estimate_cost(candidate_model,input_tokens,output_tokens,cached_tokens)

    def recommend_tier(self, prompt: str, input_tokens: int, task_complexity: str="medium", quality_target: str="balanced") -> dict[str,Any]:
        text=(prompt or "").lower()
        high_terms={"security","architecture","compliance","legal","strategy","research","incident","root cause","migration"}
        low_terms={"summarize","rewrite","format","title","short","classify","extract","draft"}
        score=min(1.0,input_tokens/12000.0)
        score+=sum(0.08 for t in high_terms if t in text)
        score-=sum(0.07 for t in low_terms if t in text)
        score += {"low":-0.18,"medium":0.0,"high":0.22}.get(task_complexity,0.0)
        if quality_target=="high": score+=.15
        score=max(0.0,min(1.0,score))
        if score>=.68: tier="premium"; model="premium-model"
        elif score<=.32: tier="economy"; model="economy-model"
        else: tier="standard"; model="standard-model"
        return {"recommended_tier":tier,"recommended_model":model,"complexity_score":round(score,3)}

    def recommendations(self, events: list[dict[str,Any]], replay_runs: list[dict[str,Any]]|None=None) -> list[dict[str,Any]]:
        if not events: return []
        replay_runs=replay_runs or []
        by_workflow=defaultdict(list)
        for e in events: by_workflow[e.get("workflow","unknown")].append(e)
        replay_by_wf=defaultdict(list)
        for r in replay_runs: replay_by_wf[r.get("workflow_id","unknown")].append(r)
        recs=[]
        for wf, rows in by_workflow.items():
            count=len(rows); spend=sum(float(r.get("estimated_cost_usd",0) or 0) for r in rows)
            inp=sum(int(r.get("input_tokens",0) or 0) for r in rows)
            out=sum(int(r.get("output_tokens",0) or 0) for r in rows)
            cached=sum(int(r.get("cached_tokens",0) or 0) for r in rows)
            retries=sum(int(r.get("retry_count",0) or 0) for r in rows)
            models=Counter(r.get("model","unknown") for r in rows)
            dominant=models.most_common(1)[0][0]
            p=self.price(dominant); tier=p.get("tier","standard")
            success=sum(1 for r in rows if r.get("success",True))/max(count,1)
            avg_input=inp/max(count,1); cache_ratio=cached/max(inp,1); avg_retries=retries/max(count,1)
            if tier=="premium" and avg_input < 12000 and success>=.9:
                target="standard-model"
                target_cost=sum(self.estimate_cost(target,int(r.get("input_tokens",0)),int(r.get("output_tokens",0)),int(r.get("cached_tokens",0))) for r in rows)
                savings=max(0.0, spend-target_cost)
                if savings>0:
                    recs.append({"id":f"route:{wf}","workflow":wf,"type":"model_rightsizing","severity":"high" if savings/spend>.4 else "medium","current_model":dominant,"candidate_model":target,"observed_spend_usd":round(spend,4),"projected_savings_usd":round(savings,4),"projected_savings_pct":round(100*savings/max(spend,1e-9),1),"confidence":"medium","reason":"Premium tier dominates a relatively compact, high-success workflow. Validate by replay before enforcement."})
            if inp>0 and cache_ratio<.15 and avg_input>5000:
                projected_cached=min(.5*inp, inp)
                p=self.price(dominant)
                per_tok_delta=(float(p["input_per_million"])-float(p.get("cached_input_per_million",p["input_per_million"])))/1_000_000
                savings=projected_cached*max(per_tok_delta,0)
                recs.append({"id":f"cache:{wf}","workflow":wf,"type":"cache_opportunity","severity":"medium","projected_savings_usd":round(savings,4),"projected_savings_pct":None,"confidence":"medium","reason":f"Only {cache_ratio:.0%} of input tokens are reported cached with {avg_input:,.0f} average input tokens."})
            if avg_input>25000:
                recs.append({"id":f"context:{wf}","workflow":wf,"type":"context_optimization","severity":"medium","projected_savings_usd":None,"projected_savings_pct":None,"confidence":"high","reason":f"Average context is {avg_input:,.0f} tokens. Review retrieval precision and repeated history."})
            if avg_retries>1.0:
                recs.append({"id":f"retry:{wf}","workflow":wf,"type":"retry_control","severity":"high","projected_savings_usd":None,"projected_savings_pct":None,"confidence":"high","reason":f"Average retries are {avg_retries:.1f} per request. Add bounded retries and failure escalation."})
        recs.sort(key=lambda x: (x.get("severity")!="high", -(x.get("projected_savings_usd") or 0)))
        return recs[:12]
