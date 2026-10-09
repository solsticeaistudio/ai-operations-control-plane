from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
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
