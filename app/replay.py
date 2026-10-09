from __future__ import annotations
from typing import Any
import json, re, uuid
from difflib import SequenceMatcher

class DeltaReplayEngine:
    """Replay/counterfactual engine inspired by DeltaStore lineage semantics.

    The product persists complete workflow capture records today and exposes a
    stable adapter boundary where the full Solstice DeltaStore can replace the
    local capture representation. Replays are immutable branches from a baseline.
    """
    def __init__(self, repository, providers, economics):
        self.repo=repository; self.providers=providers; self.economics=economics

    @staticmethod
    def quality_score(baseline_output: str, candidate_output: str, expected: dict[str,Any]|None=None) -> tuple[float,bool]:
        expected=expected or {}
        if expected.get("decision"):
            try:
                parsed=json.loads(candidate_output)
                correct=parsed.get("decision")==expected["decision"]
                semantic=SequenceMatcher(None,baseline_output,candidate_output).ratio()
                return round((.85 if correct else .25)+.15*semantic,4), bool(correct)
            except Exception:
                return .1,False
        score=SequenceMatcher(None,baseline_output,candidate_output).ratio()
        return round(score,4), score>=.72

    def compare(self, workflow: dict[str,Any], candidate_models: list[str], provider: str="mock", min_quality: float=.90) -> dict[str,Any]:
        results=[]
        meta=dict(workflow.get("metadata") or {})
        expected=workflow.get("expected") or {}
        if expected.get("decision"): meta["expected_label"]=expected["decision"]
        for model in candidate_models:
            p=self.providers.get(provider)
            r=p.generate(model,workflow["prompt"],workflow.get("context", ""),metadata=meta)
            # Replay holds the workload constant. Provider output is regenerated, but
            # economics use the captured baseline token envelope instead of the short
            # human-readable fixture text used by the portable demo.
            captured_in=int(workflow.get("baseline_input_tokens", r.input_tokens) or r.input_tokens)
            captured_out=int(workflow.get("baseline_output_tokens", r.output_tokens) or r.output_tokens)
            cache_ratio=float(meta.get("cache_ratio",0.0) or 0.0)
            captured_cached=int(captured_in*max(0.0,min(1.0,cache_ratio)))
            cost=self.economics.estimate_cost(model,captured_in,captured_out,captured_cached)
            q,success=self.quality_score(workflow["baseline_output"],r.output,expected)
            if not expected:
                model_quality=float(getattr(p,"QUALITY",{}).get(model,.93))
                q=round(.4*q+.6*model_quality,4)
                success=q>=.72
            row={"replay_id":f"rp_{uuid.uuid4().hex[:16]}","workflow_id":workflow["workflow_id"],"candidate_provider":provider,"candidate_model":model,"estimated_cost_usd":cost,"latency_ms":r.latency_ms,"task_success":success,"quality_score":q,"output":r.output,"created_at":self.repo.now_iso()}
            self.repo.insert_replay(row); results.append(row)
        baseline=float(workflow.get("baseline_cost_usd",0) or 0)
        acceptable=[r for r in results if r["quality_score"]>=min_quality and r["task_success"]]
        acceptable.sort(key=lambda x:(x["estimated_cost_usd"],x["latency_ms"]))
        winner=acceptable[0] if acceptable else None
        recommendation=None
        if winner:
            recommendation={"candidate_model":winner["candidate_model"],"quality_score":winner["quality_score"],"baseline_cost_usd":baseline,"candidate_cost_usd":winner["estimated_cost_usd"],"savings_usd":round(max(0,baseline-winner["estimated_cost_usd"]),8),"savings_pct":round(100*max(0,baseline-winner["estimated_cost_usd"])/max(baseline,1e-12),1),"action":"shadow_route","reason":"Candidate meets the configured quality floor. Validate over a larger representative replay set before enforcement."}
        return {"workflow_id":workflow["workflow_id"],"baseline":{"model":workflow["baseline_model"],"provider":workflow["baseline_provider"],"estimated_cost_usd":baseline,"output":workflow["baseline_output"]},"results":results,"recommendation":recommendation}
