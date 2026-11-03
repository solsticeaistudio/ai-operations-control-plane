"""Unit tests for the bounded limiter and local-demo exposure defaults."""
from pathlib import Path

from app.limits import FixedWindowLimiter


def test_rate_limiter_is_per_key_and_reset_after_expiry(monkeypatch):
    from app import limits

    clock = [100.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock[0])
    limiter = FixedWindowLimiter(limit=2, period_seconds=10, max_keys=2)
    assert limiter.check("tenant-a") == 0
    assert limiter.check("tenant-a") == 0
    assert limiter.check("tenant-a") == 10
    assert limiter.check("tenant-b") == 0
    assert limiter.check("tenant-c") == 10  # bounded memory, fail closed
    clock[0] = 111.0
    assert limiter.check("tenant-c") == 0  # expired windows evicted


def test_docker_demo_ports_are_localhost_only_and_api_is_unprivileged():
    repo = Path(__file__).resolve().parents[1]
    compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8080:8080"' in compose
    assert '"127.0.0.1:8123:8123"' in compose
    assert '"127.0.0.1:9000:9000"' in compose
    dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
