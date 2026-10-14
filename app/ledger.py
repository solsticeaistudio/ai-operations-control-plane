"""Tamper-evident spend ledger.

Records spend facts derived from AI usage telemetry as an append-only,
hash-chained log. This module holds the pure integrity primitives and the
``SpendLedger`` service. Persistence lives in the repository layer
(``app/repository.py``) so the same fact model can be stored by any backend.

Integrity model
---------------
Each entry chains to the previous one::

    entry_hash(n)   = sha256( previous_hash(n) || canonical_fact(n) )
    previous_hash(n)= entry_hash(n-1)
    previous_hash(0)= GENESIS_HASH

``canonical_fact`` is a deterministic, key-sorted JSON serialization of the
hashed fields (entry identity, provenance, pricing version, cost and the
spend payload). Any alteration of a stored hashed field changes the
recomputed hash and breaks the chain. Verification re-walks the chain in
sequence order and reports the first inconsistency.

A companion ledger-head record (count + head hash + head seq) is updated in
the *same transaction* as each append, which lets verification detect
truncation and tail deletion as well as in-place edits.

Known limitation: an attacker with direct write access to the whole database
could rewrite the entire log *and* the stored head, so the chain would still
verify internally. Detecting that class of rewrite requires anchoring the
head hash outside this database (e.g. periodic publication/witnessing). That
anchoring is not implemented here.

What this does NOT claim
------------------------
The ledger records spend facts *as submitted*. It does not establish that the
submitted facts are true. Identity, token counts and costs are still supplied
by the caller (see SECURITY.md). This is a record of provenance and sequence
integrity, not proof of ground truth. It must not be described as
billing-grade or enterprise-ready until the ingestion trust boundary is
actually enforced.

Concurrency
-----------
Writes are serialized by the storage engine, not by a process-local lock. A
process lock would say nothing about other processes and is intentionally not
the mechanism relied upon.

For SQLite the append runs in a single ``BEGIN IMMEDIATE`` transaction on a
dedicated connection: the read of the chain head, the insert, and the
head-record update are atomic with respect to every other writer, and a
``UNIQUE(entry_id)`` constraint enforces idempotency. Concurrency safety is
therefore a property of SQLite's own write-lock + constraints, not of Python.

The ClickHouse approach is designed but NOT implemented; see
``docs/LEDGER_DESIGN.md``. ClickHouse is not used as the ledger store because
it does not provide cross-statement row-level transactions for a hash chain.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

GENESIS_HASH = hashlib.sha256(b"solstice-aiops-spend-ledger-genesis-v1").hexdigest()

# Spend-fact fields captured from an AI usage event into the ledger payload.
SPEND_FACT_FIELDS = [
    "event_id",
    "timestamp",
    "user_id",
    "team",
    "application",
    "workflow",
    "agent",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "latency_ms",
    "estimated_cost_usd",
    "success",
    "retry_count",
    "tool_calls",
    "data_classification",
]

# Fields that participate in the hash. The spend payload is hashed as a nested
# object so a change to any captured fact breaks the chain.
HASHED_FIELDS = [
    "entry_id",
    "occurred_at",
    "recorded_at",
    "source",
    "source_event_id",
    "pricing_version",
    "cost_usd",
    "payload",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(value: Any) -> str:
    if value is None:
        return _utc_now_iso()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def canonical_material(material: Dict[str, Any]) -> str:
    """Deterministic serialization of the hashed fields."""
    return json.dumps({k: material.get(k) for k in HASHED_FIELDS}, sort_keys=True, default=str)


def compute_entry_hash(previous_hash: str, material: Dict[str, Any]) -> str:
    return hashlib.sha256((previous_hash + canonical_material(material)).encode("utf-8")).hexdigest()


def material_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the hashed material from a stored ledger row."""
    return {
        "entry_id": row["entry_id"],
        "occurred_at": row["occurred_at"],
        "recorded_at": row["recorded_at"],
        "source": row["source"],
        "source_event_id": row["source_event_id"],
        "pricing_version": row["pricing_version"],
        "cost_usd": row["cost_usd"],
        "payload": row.get("payload", {}),
    }


class SpendLedger:
    """Append-only spend ledger built on the repository abstraction."""

    def __init__(self, repo: Any):
        self.repo = repo

    @property
    def supported(self) -> bool:
        return bool(getattr(self.repo, "supports_ledger", False))

    def build_fact(self, event: Dict[str, Any], source: str, pricing_version: str) -> Dict[str, Any]:
        source_event_id = str(event.get("event_id") or "")
        entry_id = f"spend_{source_event_id}" if source_event_id else f"spend_{uuid.uuid4().hex}"
        payload = {k: event.get(k) for k in SPEND_FACT_FIELDS}
        return {
            "entry_id": entry_id,
            "occurred_at": _to_iso(event.get("timestamp")),
            "recorded_at": _utc_now_iso(),
            "source": source,
            "source_event_id": source_event_id,
            "pricing_version": pricing_version,
            "cost_usd": float(event.get("estimated_cost_usd") or 0.0),
            "payload": payload,
        }

    def record_event(self, event: Dict[str, Any], *, source: str, pricing_version: str = "unknown") -> Dict[str, Any]:
        """Record one spend fact derived from an AI usage event.

        Idempotent on the source event id: recording the same event twice
        produces a single ledger entry.
        """
        if not self.supported:
            return {
                "recorded": False,
                "supported": False,
                "reason": "ledger backend does not support append; see docs/LEDGER_DESIGN.md",
            }
        fact = self.build_fact(event, source, pricing_version)
        stored, created = self.repo.append_ledger(fact)
        return {
            "recorded": True,
            "created": created,
            "entry_id": stored["entry_id"],
            "seq": stored["seq"],
            "entry_hash": stored["entry_hash"],
        }

    def record_events(self, events: List[Dict[str, Any]], *, source: str, pricing_version: str = "unknown") -> Dict[str, Any]:
        """Record many spend facts atomically. Idempotent per source event id."""
        if not self.supported:
            return {"supported": False, "created": 0, "duplicates": 0, "reason": "ledger backend does not support append; see docs/LEDGER_DESIGN.md"}
        facts = [self.build_fact(event, source, pricing_version) for event in events]
        if hasattr(self.repo, "append_ledger_many"):
            results = self.repo.append_ledger_many(facts)
        else:
            results = [self.repo.append_ledger(fact) for fact in facts]
        created = sum(1 for _, is_created in results if is_created)
        return {"supported": True, "created": created, "duplicates": len(results) - created}

    def entries(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.repo.ledger_entries(limit)

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.ledger_entry(entry_id)

    def head(self) -> Optional[Dict[str, Any]]:
        return self.repo.ledger_meta()

    def count(self) -> int:
        return self.repo.ledger_count()

    def verify(self) -> Dict[str, Any]:
        """Re-walk the chain and check head consistency.

        Detects: altered hashed fields, altered previous_hash, deletion of any
        non-terminal entry, and truncation/tail deletion (via the stored head
        count/hash).
        """
        entries = self.repo.ledger_entries_all()
        meta = self.repo.ledger_meta()
        previous = GENESIS_HASH
        problems: List[Dict[str, Any]] = []
        checked = 0
        for row in entries:
            material = material_from_row(row)
            expected = compute_entry_hash(previous, material)
            if row.get("previous_hash") != previous:
                problems.append({
                    "seq": row.get("seq"),
                    "entry_id": row.get("entry_id"),
                    "reason": "previous_hash mismatch (entry removed or reordered)",
                })
                break
            if row.get("entry_hash") != expected:
                problems.append({
                    "seq": row.get("seq"),
                    "entry_id": row.get("entry_id"),
                    "reason": "entry_hash mismatch (altered entry)",
                })
                break
            previous = expected
            checked += 1

        if not problems and meta:
            if int(meta.get("count", -1)) != len(entries):
                problems.append({
                    "reason": "record count mismatch (entries deleted or truncated)",
                    "entries": len(entries),
                    "expected": meta.get("count"),
                })
            elif meta.get("head_hash") != previous:
                problems.append({
                    "reason": "head hash mismatch",
                    "computed": previous,
                    "expected": meta.get("head_hash"),
                })

        return {
            "ok": not problems,
            "backend": self.repo.backend,
            "checked": checked,
            "entries": len(entries),
            "head_hash": previous,
            "meta": meta,
            "problems": problems,
        }

    def reconcile(self) -> Dict[str, Any]:
        """Compare the analytical fact table against the spend ledger.

        Answers the question the ledger alone cannot: do ``ai_events`` and the
        ledger agree? Reports events with no ledger entry (a broken ingestion
        invariant), cost disagreements for a shared event id, and retained
        ledger-only history (spend recorded by the ledger whose fact row is no
        longer in ``ai_events``, e.g. after a demo reset — expected, not a
        failure).
        """
        if not self.supported:
            return {
                "supported": False,
                "ok": None,
                "reason": "ledger backend does not support append; see docs/LEDGER_DESIGN.md",
            }
        events = self.repo.billable_events()
        entries = self.repo.ledger_entries_all()
        event_cost = {e["event_id"]: float(e.get("estimated_cost_usd") or 0.0) for e in events}
        ledger_event_ids = {r.get("source_event_id") for r in entries if r.get("source_event_id")}
        missing = sorted(set(event_cost) - ledger_event_ids)
        ledger_only = sorted(ledger_event_ids - set(event_cost))
        mismatches = []
        for row in entries:
            sid = row.get("source_event_id")
            if sid in event_cost:
                ledger_cost = float(row.get("cost_usd") or 0.0)
                if abs(event_cost[sid] - ledger_cost) > 1e-9:
                    mismatches.append({
                        "event_id": sid,
                        "event_cost_usd": event_cost[sid],
                        "ledger_cost_usd": ledger_cost,
                    })
        return {
            "supported": True,
            "ok": not missing and not mismatches,
            "events": len(events),
            "ledger_entries": len(entries),
            "matched": len(set(event_cost) & ledger_event_ids),
            "missing_ledger_count": len(missing),
            "missing_ledger_entries": missing[:100],
            "ledger_only_count": len(ledger_only),
            "ledger_only_history": ledger_only[:100],
            "cost_mismatch_count": len(mismatches),
            "cost_mismatches": mismatches[:100],
            "event_total_usd": round(sum(event_cost.values()), 6),
            "ledger_total_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in entries), 6),
        }


def ingest(ledger: SpendLedger, events: List[Dict[str, Any]], *, source: str, pricing_version: str = "unknown") -> Dict[str, Any]:
    """Store usage events and (when supported) their spend facts.

    The fact table and the ledger are written atomically when the backend
    supports the ledger, so a successful call cannot leave the two disagreeing.
    The fact table is stored first-write-wins on ``event_id``, matching the
    ledger's dedup-by-``entry_id`` semantics.

    When the backend does not support the ledger the events are still stored,
    but the result carries ``ledger_protected: False``: spend was not recorded
    in a tamper-evident ledger and must not be presented as if it were.
    """
    repo = ledger.repo
    events = list(events)
    if not ledger.supported:
        for event in events:
            repo.insert_event(event)
        return {
            "ledger_protected": False,
            "source": source,
            "accepted": len(events),
            "created": 0,
            "duplicates": 0,
            "reason": "ledger backend does not support append; events stored without ledger protection; see docs/LEDGER_DESIGN.md",
        }
    facts = [ledger.build_fact(event, source, pricing_version) for event in events]
    result = repo.ingest_events_with_ledger(events, facts)
    return {
        "ledger_protected": True,
        "source": source,
        "accepted": len(events),
        "created": result["ledger_created"],
        "duplicates": result["ledger_duplicates"],
        "events_inserted": result["events_inserted"],
        "events_duplicate": result["events_duplicate"],
    }
