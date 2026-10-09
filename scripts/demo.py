from __future__ import annotations
import json, sys, httpx
BASE=sys.argv[1] if len(sys.argv)>1 else "http://localhost:8080"
def pp(x): print(json.dumps(x,indent=2))
print("\n1) Health") ; pp(httpx.get(BASE+"/health").json())
print("\n2) Reset demo telemetry") ; pp(httpx.post(BASE+"/v1/demo/seed?clear=true").json())
print("\n3) Overview") ; pp(httpx.get(BASE+"/v1/analytics/overview?hours=240").json())
print("\n4) Midas recommendations") ; pp(httpx.get(BASE+"/v1/recommendations?hours=240").json()[:3])
workflows=httpx.get(BASE+"/v1/workflows").json(); wid=workflows[0]["workflow_id"]
print("\n5) Delta-style replay") ; pp(httpx.post(BASE+f"/v1/workflows/{wid}/replay",json={"candidate_models":["economy-model","standard-model","premium-model"],"provider":"mock","min_quality_score":.90}).json())
