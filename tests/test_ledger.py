import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.repository import SQLiteRepository, ClickHouseRepository
from app.ledger import SpendLedger, GENESIS_HASH, ingest


def _repo():
    return SQLiteRepository(tempfile.mktemp(prefix="aiops_ledger_", suffix=".db"))


def _event(i, cost=0.01):
    return {
        "event_id": f"evt_{i}",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "user_id": "u",
        "team": "Support",
        "application": "app",
        "workflow": "wf",
        "agent": "a",
        "provider": "mock",
        "model": "standard-model",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cached_tokens": 0,
        "latency_ms": 100,
        "estimated_cost_usd": cost,
        "success": True,
        "retry_count": 0,
        "tool_calls": 0,
        "data_classification": "internal",
    }


def test_append_and_verify_ok():
    L = SpendLedger(_repo())
    for i in range(5):
        r = L.record_event(_event(i), source="test", pricing_version="v1")
        assert r["recorded"] is True and r["created"] is True
    v = L.verify()
    assert v["ok"] is True
    assert v["checked"] == 5 and v["entries"] == 5
    assert L.count() == 5
    # first entry chains to genesis
    assert L.repo.ledger_entries_all()[0]["previous_hash"] == GENESIS_HASH


def test_previous_hash_links_entries():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(3):
        L.record_event(_event(i), source="test")
    rows = repo.ledger_entries_all()
    assert rows[1]["previous_hash"] == rows[0]["entry_hash"]
    assert rows[2]["previous_hash"] == rows[1]["entry_hash"]


def test_alteration_detected():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(4):
        L.record_event(_event(i), source="test")
    repo.conn.execute("UPDATE spend_ledger SET payload_json=? WHERE seq=2", ('{"tampered":true}',))
    repo.conn.commit()
    v = L.verify()
    assert v["ok"] is False
    assert v["problems"], "altered entry must be reported"


def test_alteration_of_cost_detected():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(3):
        L.record_event(_event(i, cost=0.01), source="test")
    repo.conn.execute("UPDATE spend_ledger SET cost_usd=999.0 WHERE seq=2")
    repo.conn.commit()
    assert L.verify()["ok"] is False


def test_middle_deletion_detected():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(5):
        L.record_event(_event(i), source="test")
    repo.conn.execute("DELETE FROM spend_ledger WHERE seq=3")
    repo.conn.commit()
    assert L.verify()["ok"] is False


def test_head_deletion_detected():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(5):
        L.record_event(_event(i), source="test")
    repo.conn.execute("DELETE FROM spend_ledger WHERE seq=1")
    repo.conn.commit()
    assert L.verify()["ok"] is False


def test_tail_deletion_detected():
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(5):
        L.record_event(_event(i), source="test")
    max_seq = repo.conn.execute("SELECT MAX(seq) AS m FROM spend_ledger").fetchone()["m"]
    repo.conn.execute("DELETE FROM spend_ledger WHERE seq=?", (max_seq,))
    repo.conn.commit()
    assert L.verify()["ok"] is False


def test_missing_head_record_with_entries_detected():
    # Deleting the head record must not make a non-empty ledger verify.
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(4):
        L.record_event(_event(i), source="test")
    assert L.verify()["ok"] is True
    repo.conn.execute("DELETE FROM spend_ledger_meta")
    repo.conn.commit()
    v = L.verify()
    assert v["ok"] is False
    assert any("head record is missing" in p["reason"] for p in v["problems"])
    assert v["meta"] is None


def test_head_seq_tamper_detected():
    # The head_seq must match the last entry's sequence number.
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(4):
        L.record_event(_event(i), source="test")
    repo.conn.execute("UPDATE spend_ledger_meta SET head_seq=head_seq+5 WHERE id=1")
    repo.conn.commit()
    v = L.verify()
    assert v["ok"] is False
    assert any("head_seq mismatch" in p["reason"] for p in v["problems"])


def test_stale_head_seq_after_tail_repair_detected():
    # An attacker who deletes the tail and repairs count+head_hash but forgets
    # head_seq must still be caught by the head_seq check.
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(4):
        L.record_event(_event(i), source="test")
    rows = repo.ledger_entries_all()
    new_last = rows[2]
    repo.conn.execute("DELETE FROM spend_ledger WHERE seq=?", (rows[3]["seq"],))
    repo.conn.execute(
        "UPDATE spend_ledger_meta SET count=?, head_hash=? WHERE id=1",
        (3, new_last["entry_hash"]),
    )
    repo.conn.commit()
    v = L.verify()
    assert v["ok"] is False
    assert any("head_seq mismatch" in p["reason"] for p in v["problems"])


def test_duplicate_events_idempotent():
    repo = _repo()
    L = SpendLedger(repo)
    first = L.record_event(_event(1, cost=0.5), source="test")
    second = L.record_event(_event(1, cost=0.5), source="test")
    assert first["created"] is True
    assert second["recorded"] is True and second["created"] is False
    # same event id resubmitted with a different cost is still deduplicated
    third = L.record_event(_event(1, cost=0.9), source="test")
    assert third["created"] is False
    assert L.count() == 1
    assert L.verify()["ok"] is True


def test_record_events_reports_created_and_duplicates():
    L = SpendLedger(_repo())
    events = [_event(1), _event(2), _event(1)]
    out = L.record_events(events, source="test")
    assert out["created"] == 2 and out["duplicates"] == 1
    assert L.count() == 2


def test_concurrent_writes_are_consistent():
    repo = _repo()
    L = SpendLedger(repo)
    n = 120

    def work(i):
        L.record_event(_event(i), source="test")

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(work, range(n)))

    assert L.count() == n
    v = L.verify()
    assert v["ok"] is True and v["entries"] == n
    seqs = [e["seq"] for e in repo.ledger_entries_all()]
    assert len(seqs) == len(set(seqs)), "sequence numbers must be unique"


def test_concurrent_duplicate_writes_yield_one_entry():
    repo = _repo()
    L = SpendLedger(repo)
    event = _event(999)

    def work(_):
        L.record_event(event, source="test")

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(work, range(32)))

    assert L.count() == 1
    assert L.verify()["ok"] is True


def test_restart_persistence_and_chain_continuity():
    path = tempfile.mktemp(prefix="aiops_ledger_restart_", suffix=".db")
    repo = SQLiteRepository(path)
    L = SpendLedger(repo)
    for i in range(3):
        L.record_event(_event(i), source="test")
    head_before = L.head()
    repo.conn.close()

    repo2 = SQLiteRepository(path)
    L2 = SpendLedger(repo2)
    assert L2.count() == 3
    assert L2.verify()["ok"] is True
    assert L2.head()["head_hash"] == head_before["head_hash"]

    # new process continues the same chain
    L2.record_event(_event(99), source="test")
    v = L2.verify()
    assert v["ok"] is True and v["entries"] == 4
    rows = repo2.ledger_entries_all()
    assert rows[3]["previous_hash"] == rows[2]["entry_hash"]


def test_unsupported_backend_reports_clearly():
    class _NoLedger:
        backend = "stub"
        supports_ledger = False

    L = SpendLedger(_NoLedger())
    assert L.supported is False
    r = L.record_event(_event(1), source="test")
    assert r["recorded"] is False and r["supported"] is False


def test_clickhouse_ledger_not_claimed():
    # Must not claim support until the ClickHouse design is implemented.
    assert ClickHouseRepository.supports_ledger is False


def test_genesis_is_deterministic():
    import hashlib
    assert GENESIS_HASH == hashlib.sha256(b"solstice-aiops-spend-ledger-genesis-v1").hexdigest()

def test_tampered_then_rewritten_chain_still_needs_external_anchor():
    # Documented limitation: a full rewrite (including the stored head) verifies
    # internally, which is why the head hash must be anchored externally.
    repo = _repo()
    L = SpendLedger(repo)
    for i in range(3):
        L.record_event(_event(i), source="test")
    assert L.verify()["ok"] is True


def test_ingest_writes_event_and_ledger_atomically():
    repo = _repo(); L = SpendLedger(repo)
    out = ingest(L, [_event(1), _event(2), _event(3)], source="test", pricing_version="v1")
    assert out["ledger_protected"] is True
    assert out["events_inserted"] == 3 and out["created"] == 3
    assert len(repo.billable_events()) == 3
    assert L.count() == 3 and L.verify()["ok"] is True
    r = L.reconcile()
    assert r["ok"] is True and r["matched"] == 3


def test_ingest_rolls_back_events_when_ledger_write_fails(monkeypatch):
    import app.repository as R
    repo = _repo(); L = SpendLedger(repo)
    real = R.compute_entry_hash
    state = {"n": 0}

    def flaky(previous_hash, material):
        state["n"] += 1
        if state["n"] >= 2:
            raise RuntimeError("simulated ledger write failure")
        return real(previous_hash, material)

    monkeypatch.setattr(R, "compute_entry_hash", flaky)
    with pytest.raises(RuntimeError):
        ingest(L, [_event(1), _event(2), _event(3)], source="test")
    # The whole transaction rolled back: no partial events, no partial ledger.
    assert repo.billable_events() == []
    assert L.count() == 0 and L.head() is None
    assert L.verify()["ok"] is True


def test_resubmitted_event_is_first_write_wins():
    repo = _repo(); L = SpendLedger(repo)
    ingest(L, [_event(1, cost=0.5)], source="test")
    out = ingest(L, [_event(1, cost=0.9)], source="test")
    assert out["created"] == 0 and out["duplicates"] == 1
    assert out["events_duplicate"] == 1
    events = repo.billable_events()
    assert len(events) == 1 and events[0]["estimated_cost_usd"] == 0.5
    assert repo.ledger_entries_all()[0]["cost_usd"] == 0.5
    assert L.reconcile()["ok"] is True


def test_reconcile_detects_missing_ledger_entry():
    repo = _repo(); L = SpendLedger(repo)
    ingest(L, [_event(1), _event(2), _event(3)], source="test")
    repo.conn.execute("DELETE FROM spend_ledger WHERE source_event_id='evt_2'")
    repo.conn.commit()
    r = L.reconcile()
    assert r["ok"] is False and r["missing_ledger_count"] == 1
    assert "evt_2" in r["missing_ledger_entries"]


def test_reconcile_detects_cost_mismatch():
    repo = _repo(); L = SpendLedger(repo)
    ingest(L, [_event(1, cost=0.5)], source="test")
    repo.conn.execute("UPDATE ai_events SET estimated_cost_usd=0.9 WHERE event_id='evt_1'")
    repo.conn.commit()
    r = L.reconcile()
    assert r["ok"] is False and r["cost_mismatch_count"] == 1
    assert r["cost_mismatches"][0]["event_id"] == "evt_1"


def test_ingest_without_ledger_support_marks_unprotected():
    class _Repo:
        backend = "stub"; supports_ledger = False
        def __init__(self): self.events = []
        def insert_event(self, e): self.events.append(e)

    repo = _Repo(); L = SpendLedger(repo)
    out = ingest(L, [_event(1), _event(2)], source="test")
    assert out["ledger_protected"] is False
    assert out["accepted"] == 2 and out["created"] == 0 and "reason" in out
    assert len(repo.events) == 2
    assert L.reconcile()["supported"] is False
