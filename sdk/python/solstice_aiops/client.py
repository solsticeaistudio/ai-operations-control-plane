from __future__ import annotations
import httpx
class AIopsClient:
    def __init__(self,base_url="http://localhost:8080",timeout=20): self.base_url=base_url.rstrip("/"); self.client=httpx.Client(timeout=timeout)
    def emit(self,**event):
        r=self.client.post(self.base_url+"/v1/events",json=event); r.raise_for_status(); return r.json()
    def generate(self,**request):
        r=self.client.post(self.base_url+"/v1/gateway/generate",json=request); r.raise_for_status(); return r.json()
    def overview(self,hours=24):
        r=self.client.get(self.base_url+"/v1/analytics/overview",params={"hours":hours}); r.raise_for_status(); return r.json()
