from app.economics import MidasEconomics
from app.config import settings

def test_cached_input_is_cheaper():
    m=MidasEconomics(settings.pricing_file)
    a=m.estimate_cost("premium-model",10000,1000,0)
    b=m.estimate_cost("premium-model",10000,1000,8000)
    assert b<a

def test_low_complexity_routes_economy():
    m=MidasEconomics(settings.pricing_file)
    r=m.recommend_tier("Summarize this short email",800,"low")
    assert r["recommended_tier"]=="economy"
