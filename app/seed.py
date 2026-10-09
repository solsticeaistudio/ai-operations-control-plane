from __future__ import annotations
from datetime import datetime, timezone, timedelta
import random, uuid
from .models import AIEvent, WorkflowCapture

TEAMS=[("Support","ticket-triage","support-copilot","premium-model",.97), ("Engineering","code-review","dev-assistant","premium-model",.96), ("Sales","account-research","sales-assistant","standard-model",.94), ("Marketing","copy-draft","content-assistant","premium-model",.98), ("Finance","invoice-extract","finance-ops","standard-model",.95)]

def seed_demo(repo,economics,clear=True,seed=7):
    rnd=random.Random(seed)
    if clear: repo.clear_demo()
    now=datetime.now(timezone.utc)
    users={t:[f"{t.lower()}-{i:02d}" for i in range(1,9)] for t,_,_,_,_ in TEAMS}
    for day in range(9,-1,-1):
        for team,wf,app,model,base_success in TEAMS:
            volume={"Support":26,"Engineering":18,"Sales":14,"Marketing":12,"Finance":10}[team]
            for _ in range(volume):
                inp=int(rnd.gauss({"Support":8200,"Engineering":14500,"Sales":6200,"Marketing":3600,"Finance":9400}[team],1800)); inp=max(800,inp)
                out=max(80,int(rnd.gauss(520,180)))
                cache_ratio={"Support":.08,"Engineering":.31,"Sales":.22,"Marketing":.06,"Finance":.38}[team]
                cached=int(inp*max(0,min(.8,rnd.gauss(cache_ratio,.08))))
                retries=0 if rnd.random()>.10 else rnd.choice([1,1,2])
                success=rnd.random()<base_success
                cost=economics.estimate_cost(model,inp,out,cached)
                ts=now-timedelta(days=day,hours=rnd.random()*12,minutes=rnd.random()*50)
                policy_reasons=[]
                if team=="Marketing" and rnd.random()<.035: policy_reasons=["Provider allowlist review required"]
                e=AIEvent(timestamp=ts,user_id=rnd.choice(users[team]),team=team,application=app,workflow=wf,agent=app,provider="mock",model=model,input_tokens=inp,output_tokens=out,cached_tokens=cached,latency_ms=int(rnd.gauss(980 if model=="premium-model" else 650,120)),estimated_cost_usd=cost,success=True,tool_calls=rnd.randint(0,3),retry_count=retries,data_classification="restricted" if team=="Finance" else "internal",policy_decision="allow",policy_reasons=policy_reasons,task_success=success,quality_score=round(rnd.uniform(.91,.99),3) if success else round(rnd.uniform(.55,.82),3),metadata={"source":"seed","difficulty":.35 if team in ("Marketing","Sales") else .55})
                repo.insert_event(e.model_dump())
    # policies
    repo.upsert_policy({"name":"Enterprise provider & retry baseline","mode":"shadow","priority":10,"enabled":True,"scope":{},"rules":{"allowed_providers":["mock","openai_compatible"],"max_retries":2,"max_tool_calls":6,"max_input_tokens":40000,"route_low_complexity_to":"economy-model","restricted_data_providers":{"restricted":["mock"]}}})
    repo.upsert_budget({"scope_type":"team","scope_value":"Support","monthly_limit_usd":4.0,"warning_threshold":.80,"hard_limit":False,"overage_action":"warn"})
    repo.upsert_budget({"scope_type":"team","scope_value":"Marketing","monthly_limit_usd":1.5,"warning_threshold":.80,"hard_limit":False,"overage_action":"downgrade"})
    # captured replay workflows
    captures=[
      WorkflowCapture(name="Support ticket triage",prompt="Classify the support ticket and decide whether it should be resolved automatically or escalated.",context="Customer reports intermittent SSO failures after an IdP certificate rotation. Prior incidents indicate certificate propagation can take several minutes.",baseline_provider="mock",baseline_model="premium-model",baseline_output='{"confidence": 0.94, "decision": "escalate", "model": "premium-model", "summary": "Escalate based on the supplied workflow context."}',baseline_input_tokens=8800,baseline_output_tokens=260,baseline_cost_usd=economics.estimate_cost("premium-model",8800,260,600),expected={"decision":"escalate"},metadata={"difficulty":.52,"cache_ratio":.08,"source":"seed"}),
      WorkflowCapture(name="Marketing copy summary",prompt="Summarize the campaign brief into three concise bullets.",context="Q4 product launch brief with approved claims, audience, CTA and channel notes.",baseline_provider="mock",baseline_model="premium-model",baseline_output="High-confidence response from premium-model: Summarize the campaign brief into three concise bullets.",baseline_input_tokens=3900,baseline_output_tokens=180,baseline_cost_usd=economics.estimate_cost("premium-model",3900,180,120),expected={},metadata={"difficulty":.18,"cache_ratio":.05,"source":"seed"})]
    for c in captures: repo.insert_workflow(c.model_dump())
    repo.audit("seed","system","demo-data","created",{"events":sum({"Support":26,"Engineering":18,"Sales":14,"Marketing":12,"Finance":10}.values())*10,"workflows":len(captures)})
    return {"ok":True,"events":800,"workflows":len(captures)}
