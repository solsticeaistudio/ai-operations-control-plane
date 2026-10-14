# Spend ledger — design, integrity model and trust boundaries

Status: **SQLite path implemented and tested. ClickHouse path designed, not implemented.**
This document is the source of truth for what the spend ledger does and does not guarantee.

## Purpose

The spend ledger records spend facts derived from AI usage events as an
append-only, hash-chained sequence. It exists so that the *sequence and content*
of recorded spend facts can be shown to be intact after the fact, and so that a
resubmitted event cannot silently double-count.

It is deliberately separate from the analytical tables (`ai_events`) and from the
dashboard, so adding the ledger does not change existing telemetry, analytics or
replay behavior.

## Integrity model

Each entry chains to its predecessor:

```
entry_hash(n)   = sha256( previous_hash(n) || canonical_fact(n) )
previous_hash(n)= entry_hash(n-1)
previous_hash(0)= GENESIS_HASH   # sha256("solstice-aiops-spend-ledger-genesis-v1")
```

`canonical_fact` is a key-sorted JSON serialization of the hashed fields:

- `entry_id` — deterministic id derived from the source event id (`spend_<event_id>`)
- `occurred_at` — the time of the spend fact
- `recorded_at` — the time it was written to the ledger
- `source` — provenance of ingestion (e.g. `api:/v1/events`, `gateway:/v1/gateway/generate`, `seed`)
- `source_event_id` — identity of the originating event
- `pricing_version` — the pricing configuration used to compute the cost
- `cost_usd` — estimated cost
- `payload` — the captured spend facts (team, app, workflow, provider, model, tokens, …)

A companion **head record** (`count`, `head_seq`, `head_hash`) is updated in the
**same transaction** as every append.

`GET /v1/ledger/verify` re-walks the chain in sequence order and compares it to
the head record.

### What verification detects

- **Alteration** of any hashed field (payload, cost, provenance, pricing version,
  timestamps) — recomputed hash no longer matches.
- **Reordering / previous_hash tampering** — chain link mismatch.
- **Deletion of any entry, including the last** — an interior gap breaks the
  `previous_hash` link; removing the tail (or truncating) is caught by the head
  record's `count`/`head_hash`.
- **Duplicates** — a resubmitted event id does not create a second entry.

### What verification does NOT guarantee (stated plainly)

- **Ground truth.** The ledger records the spend facts *as submitted*. Identity,
  token counts and costs are supplied by the caller. A client with a valid API
  key can submit a low token count or a zero cost leg and the ledger will
  faithfully chain it. This is a **provenance and sequence-integrity** record,
  not proof the numbers are correct.
- **Protection against full rewrite.** An attacker with direct write access to the
  whole SQLite file can rewrite the entries *and* the stored head so the chain
  still verifies internally. Detecting that requires anchoring the head hash
  outside the database (periodic publication/witnessing/logging). That anchoring
  is **not implemented**.
- **Multi-writer distributed safety.** The implemented store is a single SQLite
  file. There is no distributed consensus.

Because of these limits, the ledger must **not** be described as billing-grade or
enterprise-ready. It becomes billable only once the ingestion trust boundary is
enforced (see SECURITY.md) and the head is externally anchored.

## Ingestion trust boundary (not yet enforced)

Trust flows in this order today:

```
caller → API key (optional) → self-asserted identity/tokens/cost → ledger
```

The ledger faithfully records whatever the caller supplies. Until identity is
derived from a trusted gateway and token/cost values are independently
established, the ledger is an integrity record over *submitted* facts.
This is the same limitation already documented in SECURITY.md.

## Concurrency model (SQLite)

Correctness does **not** rely on a Python/process-local lock, which would say
nothing about other processes.

Each append runs on a **dedicated connection** in a single
`BEGIN IMMEDIATE` transaction:

1. take SQLite's write lock (`BEGIN IMMEDIATE`),
2. read the chain head,
3. insert the new entry (or detect a duplicate),
4. update the head record,
5. `COMMIT`.

SQLite serializes writers through its own file/write lock; `busy_timeout=30000`
bounds contention. Idempotency is enforced by a
`UNIQUE(entry_id)` constraint, so two concurrent submissions of the same event
cannot create two rows. The database does not run in a Python lock; the guarantee
is a property of SQLite's transaction + constraint semantics, and is covered by a
concurrency test (`tests/test_ledger.py::test_concurrent_writes_are_consistent`,
`test_concurrent_duplicate_writes_yield_one_entry`).

Bulk ingestion (seed, batch API) uses `append_ledger_many`, which performs the
whole batch in one `BEGIN IMMEDIATE` transaction with the same guarantees.

`PRAGMA journal_mode=WAL` is enabled so readers (analytics/dashboard) are not
blocked by a writer.

### Known SQLite caveat

`BEGIN IMMEDIATE` serializes writers; a batch holds the write lock for its
duration. This is fine for current volumes but should be revisited if a single
batch becomes very large.

## Ingestion consistency (fact table ↔ ledger)

The ledger is only trustworthy if it cannot silently disagree with the
analytical fact table it is derived from. Ingestion guarantees that:

- **Atomic.** Event rows (`ai_events`) and their spend facts are written in a
  single `BEGIN IMMEDIATE` transaction (`SQLiteRepository.ingest_events_with_ledger`,
  driven by `ledger.ingest(...)`). If the ledger append fails, the event rows
  roll back too. A successful call cannot leave an event with no ledger entry.
- **First-write-wins.** Events are inserted with `ON CONFLICT(event_id) DO
  NOTHING`, matching the ledger's dedup-by-`entry_id` policy. Resubmitting an
  `event_id` with a different cost no longer overwrites the stored fact (which
  previously could make analytics totals diverge from the ledger). The first
  value wins in both tables.
- **Explicitly unprotected when unsupported.** On a backend without ledger
  support (e.g. ClickHouse) events are still stored, but the ingest result
  carries `ledger_protected: false`. Spend is never silently presented as
  ledger-protected.

`GET /v1/ledger/reconcile` reports the current agreement: events with no ledger
entry (`missing_ledger_count`, a broken invariant) and cost disagreements for a
shared event id (`cost_mismatch_count`). Retained ledger entries whose fact row
is no longer present are reported as `ledger_only_history` (expected after a
demo reset) and do **not** make `ok` false.

### Demo reset does not touch the ledger

`clear_demo` clears **only** rows previously registered as demo, tracked in a
`demo_records(table_name, record_id)` registry that the seeder populates. It
never deletes real telemetry, policies, budgets or workflows, and it never
deletes ledger rows or the head record: wiping them on a demo reset would
destroy retained (possibly non-demo) history and mask deletion by removing the
head. Seeded events use **deterministic ids** (`seed_<day>_<team>_<n>`), so
re-seeding reinserts the same events without creating duplicate ledger entries.

`POST /v1/demo/seed` is additionally gated on `AIOPS_DEMO_MODE` and returns
`403` when demo mode is off. Backends that cannot scope deletes to demo rows
(ClickHouse) set `supports_demo_scoping = false`; they refuse to clear
(`NotImplementedError`) and the seeder skips clearing on them rather than
truncating tables.

## Idempotency

`entry_id = "spend_" + source_event_id`. Resubmitting the same event id with the
same or different payload is a no-op after the first write. Events without an id
are given a random id and are therefore not deduplicated — callers that need
idempotency must supply a stable `event_id`.

## Pricing provenance

Each entry stores the `pricing_version` used to compute its cost. The pricing
table is configuration (`config/pricing.json`), so costs can be recomputed from
facts without rewriting history (facts vs. policy separation).

## Storage schema (SQLite)

```sql
spend_ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id UNIQUE, occurred_at,
             recorded_at, source, source_event_id, pricing_version, cost_usd,
             payload_json, previous_hash, entry_hash)
spend_ledger_meta(id=1, count, head_seq, head_hash, updated_at)
```

`AUTOINCREMENT` ensures sequence numbers are never reused, so a tail deletion
cannot be masked by a subsequent insert reusing the same `seq`.

## ClickHouse — designed, NOT implemented

ClickHouse is **not** used as the ledger store today. The read/verify methods on
the ClickHouse repository raise `NotImplementedError`, and `supports_ledger` is
`False`. The API returns `501` when the storage backend does not support the
ledger.

**Why not:** ClickHouse provides no multi-statement, row-level transactions that
would let an append read the chain head, insert, and update the head record
atomically. A hash chain written through concurrent inserts offers no atomic
append guarantee, so claiming tamper-evidence on ClickHouse today would be false.

**Proposed approach (future, unvalidated):**

1. Keep the authoritative ledger in a transactional single-writer store (SQLite,
   or Postgres with `SERIALIZABLE`/row locks). The ledger is intentionally small
   (one row per event, hashes only).
2. Treat ClickHouse as an **analytical projection** of the ledger, regenerated
   from the authoritative store — not as the ledger itself.
3. If the ledger must live in ClickHouse, introduce an explicit single-writer
   sequencer (one process/partition owns appends) and anchor the head hash outside
   ClickHouse. This is a separate design that must be validated before it is
   claimed.

Until that is built and tested, ClickHouse deployments have telemetry, analytics,
replay and the existing APIs, but **no spend ledger**.

## API surface added

- `GET /v1/ledger?limit=` — recent entries (newest first)
- `GET /v1/ledger/{entry_id}` — one entry
- `GET /v1/ledger/head` — head record + count
- `GET /v1/ledger/verify` — chain verification result
- `GET /v1/ledger/reconcile` — fact-table ↔ ledger agreement report

Ingest endpoints (`POST /v1/events`, `POST /v1/events/batch`,
`POST /v1/gateway/generate`) now also append to the ledger atomically with the
event write. Their existing response fields are unchanged; a `ledger` object is
added (`ledger_protected`, `source`, `accepted`, `created`, `duplicates`).

## Test coverage

`tests/test_ledger.py`: append/verify, previous-hash linkage, alteration,
malicious middle/head/tail deletion, duplicate-event idempotency, concurrent
writes, concurrent duplicate writes, restart persistence and chain continuity,
unsupported-backend behavior, ClickHouse non-claim, atomic ingest, rollback of
both writes on ledger failure, first-write-wins drift prevention, reconciliation
(missing entry, cost mismatch), and unsupported ingest reporting.

`tests/test_api.py`: ledger endpoints over seeded data, gateway → ledger,
ingest idempotency, fact/ledger agreement, resubmitted-cost immutability, demo
reset preserving ledger history, demo reset preserving real telemetry/policies/
budgets, demo operations disabled outside demo mode, reconciliation over seeded
data, and confirmation that existing analytics/recommendations/budgets endpoints
are unchanged.

`tests/test_sdk.py`: the Python SDK (`emit`, `generate`, `overview`) still works
against the real app after the ingest response gained the `ledger_protected`
field and count-based `created`/`duplicates`.
