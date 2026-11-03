"""Isolated storage tests. These do not require a live ClickHouse server."""
from dataclasses import replace

import pytest

from app import repository
from app.config import settings


def test_secure_clickhouse_requires_tls(tmp_path):
    config = replace(
        settings, storage="clickhouse", secure_mode=True,
        clickhouse_url="http://clickhouse.internal:8123",
    )
    with pytest.raises(RuntimeError, match="TLS"):
        repository.create_repository(config, tenant_id="tenant-a")


def test_secure_clickhouse_does_not_fallback_to_sqlite(monkeypatch, tmp_path):
    def broken_clickhouse(*args, **kwargs):
        raise ConnectionError("ClickHouse unavailable")

    monkeypatch.setattr(repository, "ClickHouseRepository", broken_clickhouse)
    config = replace(
        settings, storage="clickhouse", secure_mode=True,
        clickhouse_url="https://clickhouse.internal:8443",
        sqlite_path=str(tmp_path / "should-not-exist.db"),
        allow_storage_fallback=True,
    )
    with pytest.raises(ConnectionError, match="ClickHouse unavailable"):
        repository.create_repository(config, tenant_id="tenant-a")
    assert not (tmp_path / "should-not-exist.db").exists()


def test_clickhouse_tenants_get_distinct_database_names(monkeypatch):
    constructed = []

    class RecordingRepository:
        def __init__(self, url, database, user, password):
            self.database = database
            constructed.append(database)

    monkeypatch.setattr(repository, "ClickHouseRepository", RecordingRepository)
    config = replace(
        settings, storage="clickhouse", secure_mode=True,
        clickhouse_database="aiops", clickhouse_url="https://clickhouse.internal:8443",
    )
    a = repository.create_repository(config, tenant_id="tenant-a")
    b = repository.create_repository(config, tenant_id="tenant-b")
    assert a.database.startswith("aiops_t_")
    assert b.database.startswith("aiops_t_")
    assert a.database != b.database
    assert constructed == [a.database, b.database]


def test_secure_storage_requires_trusted_tenant(tmp_path):
    config = replace(
        settings, storage="sqlite", secure_mode=True,
        sqlite_path=str(tmp_path / "secure.db"),
    )
    with pytest.raises(RuntimeError, match="trusted tenant"):
        repository.create_repository(config)
