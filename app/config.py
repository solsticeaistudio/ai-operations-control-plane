from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    # Deployment environment. "development" keeps the convenient, insecure
    # local defaults (open API, demo mode on, silent storage fallback).
    # "production" is fail-closed: see validate_settings().
    aiops_env: str = os.getenv("AIOPS_ENV", "development").strip().lower()
    storage: str = os.getenv("AIOPS_STORAGE", "sqlite").lower()
    sqlite_path: str = os.getenv("AIOPS_SQLITE_PATH", "./data/aiops.db")
    clickhouse_url: str = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
    clickhouse_database: str = os.getenv("CLICKHOUSE_DATABASE", "aiops")
    clickhouse_user: str = os.getenv("CLICKHOUSE_USER", "default")
    clickhouse_password: str = os.getenv("CLICKHOUSE_PASSWORD", "")
    allow_storage_fallback: bool = os.getenv("ALLOW_STORAGE_FALLBACK", "true").lower() == "true"
    demo_mode: bool = os.getenv("AIOPS_DEMO_MODE", "true").lower() == "true"
    api_key: str = os.getenv("AIOPS_API_KEY", "")
    pricing_file: str = os.getenv("AIOPS_PRICING_FILE", str(Path(__file__).resolve().parents[1] / "config" / "pricing.json"))
    openai_compat_base_url: str = os.getenv("OPENAI_COMPAT_BASE_URL", "")
    openai_compat_api_key: str = os.getenv("OPENAI_COMPAT_API_KEY", "")

settings = Settings()


def validate_settings(s: Settings) -> None:
    """Fail-closed guard for customer/production deployments.

    The local defaults are deliberately convenient and deliberately insecure:
    the product API is open (no ``AIOPS_API_KEY``), demo mode is on, and a
    failed ClickHouse connection silently falls back to SQLite. None of those
    are acceptable for a customer deployment, so when ``AIOPS_ENV=production``
    the process refuses to start unless every dangerous default is overridden.

    This is opt-in via ``AIOPS_ENV`` so local demos and the test suite keep
    working unchanged. See SECURITY.md.
    """
    if (s.aiops_env or "").strip().lower() != "production":
        return
    problems = []
    if not s.api_key:
        problems.append("AIOPS_API_KEY must be set (the product API must not be open)")
    if s.demo_mode:
        problems.append("AIOPS_DEMO_MODE must be false (demo seeding must not inject fabricated data)")
    if s.allow_storage_fallback:
        problems.append("ALLOW_STORAGE_FALLBACK must be false (no silent fallback to a different store)")
    if problems:
        raise RuntimeError(
            "Refusing to start: unsafe production configuration (AIOPS_ENV=production). "
            + "; ".join(problems)
            + ". See .env.production.example and SECURITY.md."
        )
