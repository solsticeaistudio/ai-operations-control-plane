from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import hashlib, json, time, httpx

@dataclass
class ProviderResult:
    output: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    success: bool=True
    error_code: str=""

class ProviderAdapter:
    provider_id="base"
    def generate(self, model: str, prompt: str, context: str="", max_output_tokens: int=512, metadata: dict[str,Any]|None=None) -> ProviderResult:
        raise NotImplementedError

class MockProvider(ProviderAdapter):
    provider_id="mock"
    QUALITY={"economy-model":.84,"standard-model":.96,"premium-model":.995,"gemini-2.5-flash":.88,"gpt-4o":.99}
    LATENCY={"economy-model":380,"standard-model":720,"premium-model":1180,"gemini-2.5-flash":420,"gpt-4o":1200}
    def _tokens(self,text:str)->int: return max(1, int(len(text.split())*1.32))
    def generate(self, model: str, prompt: str, context: str="", max_output_tokens: int=512, metadata=None) -> ProviderResult:
        metadata=metadata or {}; started=time.perf_counter()
        quality=self.QUALITY.get(model,.93)
        difficulty=float(metadata.get("difficulty",.45))
        h=int(hashlib.sha256((model+prompt+context).encode()).hexdigest()[:8],16)/0xffffffff
        expected=metadata.get("expected_label")
        success=(quality - difficulty*.14 + h*.08) >= .77
        if expected:
            label=expected if success else ("escalate" if expected!="escalate" else "review")
            payload={"decision":label,"confidence":round(max(.51,min(.99,quality-difficulty*.1)),2),"summary":f"{label.title()} based on the supplied workflow context.","model":model}
            output=json.dumps(payload,sort_keys=True)
        else:
            prefix="High-confidence" if quality>.97 else "Practical" if quality>.9 else "Concise"
            output=f"{prefix} response from {model}: " + (prompt.strip()[:220] or "No prompt supplied")
        input_tokens=self._tokens(prompt+" "+context)
        output_tokens=min(max_output_tokens,self._tokens(output))
        cache_hint=float(metadata.get("cache_ratio",0.0)); cached=int(input_tokens*max(0,min(1,cache_hint)))
        latency=self.LATENCY.get(model,700)+int(difficulty*220)+int(h*90)
        # Do not actually sleep in demo/provider tests.
        return ProviderResult(output,input_tokens,output_tokens,cached,latency,True,"")

class OpenAICompatibleProvider(ProviderAdapter):
    provider_id="openai_compatible"
    def __init__(self, base_url: str, api_key: str):
        self.base_url=base_url.rstrip("/"); self.api_key=api_key
    def generate(self, model: str, prompt: str, context: str="", max_output_tokens: int=512, metadata=None) -> ProviderResult:
        headers={"Authorization":f"Bearer {self.api_key}"} if self.api_key else {}
        messages=[]
        if context: messages.append({"role":"system","content":context})
        messages.append({"role":"user","content":prompt})
        started=time.perf_counter()
        r=httpx.post(f"{self.base_url}/chat/completions",headers=headers,json={"model":model,"messages":messages,"max_tokens":max_output_tokens},timeout=60)
        latency=int((time.perf_counter()-started)*1000); r.raise_for_status(); data=r.json()
        usage=data.get("usage") or {}; choice=(data.get("choices") or [{}])[0]
        output=((choice.get("message") or {}).get("content") or "")
        details=usage.get("prompt_tokens_details") or {}
        return ProviderResult(output,int(usage.get("prompt_tokens",0)),int(usage.get("completion_tokens",0)),int(details.get("cached_tokens",0) or 0),latency,True,"")

class ProviderRegistry:
    def __init__(self, openai_base_url: str="", openai_api_key: str=""):
        self.providers={"mock":MockProvider()}
        if openai_base_url:
            self.providers["openai_compatible"]=OpenAICompatibleProvider(openai_base_url,openai_api_key)
    def get(self, provider: str)->ProviderAdapter:
        if provider not in self.providers: raise KeyError(f"Provider '{provider}' is not configured")
        return self.providers[provider]
