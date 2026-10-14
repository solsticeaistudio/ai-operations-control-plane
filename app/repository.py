from __future__ import annotations
from typing import Any
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json, sqlite3, uuid, httpx, urllib.parse

from .ledger import GENESIS_HASH, compute_entry_hash

class Repository:
    backend="base"
    supports_ledger=False
    # True only where demo data can be identified and cleared precisely. A
    # backend without this must never clear tables wholesale on a demo reset.
    supports_demo_scoping=False
    @staticmethod
    def now_iso(): return datetime.now(timezone.utc).isoformat()

class SQLiteRepository(Repository):
    backend="sqlite"
    supports_ledger=True
    supports_demo_scoping=True
    def __init__(self,path:str):
        self.path=path; Path(path).parent.mkdir(parents=True,exist_ok=True); self.conn=sqlite3.connect(path,check_same_thread=False); self.conn.row_factory=sqlite3.Row
        # WAL lets readers proceed during a writer; busy_timeout bounds lock contention.
        self.conn.execute("PRAGMA journal_mode=WAL"); self.conn.execute("PRAGMA busy_timeout=30000")
        self.init_schema()
    def init_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_events(event_id TEXT PRIMARY KEY,timestamp TEXT,request_id TEXT,trace_id TEXT,session_id TEXT,user_id TEXT,team TEXT,application TEXT,workflow TEXT,agent TEXT,provider TEXT,model TEXT,input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER,latency_ms INTEGER,estimated_cost_usd REAL,success INTEGER,error_code TEXT,tool_calls INTEGER,retry_count INTEGER,data_classification TEXT,policy_decision TEXT,policy_reasons_json TEXT,task_success INTEGER,quality_score REAL,metadata_json TEXT);
        CREATE TABLE IF NOT EXISTS workflows(workflow_id TEXT PRIMARY KEY,captured_at TEXT,name TEXT,prompt TEXT,context TEXT,baseline_provider TEXT,baseline_model TEXT,baseline_output TEXT,baseline_input_tokens INTEGER,baseline_output_tokens INTEGER,baseline_cost_usd REAL,expected_json TEXT,tool_trace_json TEXT,metadata_json TEXT);
        CREATE TABLE IF NOT EXISTS replay_runs(replay_id TEXT PRIMARY KEY,workflow_id TEXT,candidate_provider TEXT,candidate_model TEXT,estimated_cost_usd REAL,latency_ms INTEGER,task_success INTEGER,quality_score REAL,output TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS policies(policy_id TEXT PRIMARY KEY,name TEXT,mode TEXT,priority INTEGER,enabled INTEGER,scope_json TEXT,rules_json TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS budgets(budget_id TEXT PRIMARY KEY,scope_type TEXT,scope_value TEXT,monthly_limit_usd REAL,warning_threshold REAL,hard_limit INTEGER,overage_action TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS audit(audit_id TEXT PRIMARY KEY,timestamp TEXT,kind TEXT,actor TEXT,subject TEXT,decision TEXT,details_json TEXT);
        CREATE TABLE IF NOT EXISTS spend_ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT,entry_id TEXT NOT NULL UNIQUE,occurred_at TEXT,recorded_at TEXT NOT NULL,source TEXT NOT NULL,source_event_id TEXT,pricing_version TEXT,cost_usd REAL NOT NULL DEFAULT 0,payload_json TEXT NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS spend_ledger_meta(id INTEGER PRIMARY KEY CHECK(id=1),count INTEGER NOT NULL,head_seq INTEGER,head_hash TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS demo_records(table_name TEXT NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(table_name,record_id));
        """); self.conn.commit()

    # -- spend ledger ------------------------------------------------------
    def _open_ledger_conn(self):
        # Dedicated autocommit connection so BEGIN IMMEDIATE controls the
        # whole append transaction. Correctness comes from SQLite's write
        # lock + the UNIQUE(entry_id) constraint, not from a Python lock.
        conn=sqlite3.connect(self.path,isolation_level=None,timeout=30.0,check_same_thread=False)
        conn.row_factory=sqlite3.Row; conn.execute("PRAGMA busy_timeout=30000")
        return conn
    def append_ledger(self,fact):
        entry_id=fact["entry_id"]; payload=fact.get("payload") or {}
        payload_json=json.dumps(payload,sort_keys=True,default=str)
        conn=self._open_ledger_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT * FROM spend_ledger WHERE entry_id=?",(entry_id,)).fetchone()
            if existing is not None:
                conn.execute("COMMIT"); return self._ledger_row(existing), False
            last=conn.execute("SELECT entry_hash FROM spend_ledger ORDER BY seq DESC LIMIT 1").fetchone()
            previous_hash=last["entry_hash"] if last is not None else GENESIS_HASH
            material={"entry_id":entry_id,"occurred_at":fact.get("occurred_at"),"recorded_at":fact.get("recorded_at"),"source":fact.get("source"),"source_event_id":fact.get("source_event_id"),"pricing_version":fact.get("pricing_version"),"cost_usd":float(fact.get("cost_usd") or 0.0),"payload":payload}
            entry_hash=compute_entry_hash(previous_hash,material)
            cur=conn.execute("INSERT INTO spend_ledger(entry_id,occurred_at,recorded_at,source,source_event_id,pricing_version,cost_usd,payload_json,previous_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",(entry_id,material["occurred_at"],material["recorded_at"],material["source"],material["source_event_id"],material["pricing_version"],material["cost_usd"],payload_json,previous_hash,entry_hash))
            seq=cur.lastrowid
            meta=conn.execute("SELECT count FROM spend_ledger_meta WHERE id=1").fetchone()
            count=(meta["count"] if meta is not None else 0)+1
            conn.execute("INSERT OR REPLACE INTO spend_ledger_meta(id,count,head_seq,head_hash,updated_at) VALUES(1,?,?,?,?)",(count,seq,entry_hash,self.now_iso()))
            conn.execute("COMMIT")
            return {"seq":seq,"entry_id":entry_id,"occurred_at":material["occurred_at"],"recorded_at":material["recorded_at"],"source":material["source"],"source_event_id":material["source_event_id"],"pricing_version":material["pricing_version"],"cost_usd":material["cost_usd"],"payload":payload,"previous_hash":previous_hash,"entry_hash":entry_hash}, True
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally:
            conn.close()
    def append_ledger_many(self,facts):
        """Append several facts in one BEGIN IMMEDIATE transaction.

        Faster than calling append_ledger per fact for bulk ingestion (seed,
        batch API) while keeping the same atomicity, ordering, idempotency and
        chain guarantees. Returns a list of (stored_row, created) pairs.
        """
        fact_list=list(facts)
        if not fact_list: return []
        conn=self._open_ledger_conn(); results=[]
        try:
            conn.execute("BEGIN IMMEDIATE")
            last=conn.execute("SELECT entry_hash FROM spend_ledger ORDER BY seq DESC LIMIT 1").fetchone()
            previous_hash=last["entry_hash"] if last is not None else GENESIS_HASH
            meta=conn.execute("SELECT count,head_seq FROM spend_ledger_meta WHERE id=1").fetchone()
            count=meta["count"] if meta is not None else 0; head_seq=meta["head_seq"] if meta is not None else None
            for fact in fact_list:
                entry_id=fact["entry_id"]; payload=fact.get("payload") or {}
                payload_json=json.dumps(payload,sort_keys=True,default=str)
                existing=conn.execute("SELECT * FROM spend_ledger WHERE entry_id=?",(entry_id,)).fetchone()
                if existing is not None:
                    results.append((self._ledger_row(existing),False)); continue
                material={"entry_id":entry_id,"occurred_at":fact.get("occurred_at"),"recorded_at":fact.get("recorded_at"),"source":fact.get("source"),"source_event_id":fact.get("source_event_id"),"pricing_version":fact.get("pricing_version"),"cost_usd":float(fact.get("cost_usd") or 0.0),"payload":payload}
                entry_hash=compute_entry_hash(previous_hash,material)
                cur=conn.execute("INSERT INTO spend_ledger(entry_id,occurred_at,recorded_at,source,source_event_id,pricing_version,cost_usd,payload_json,previous_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",(entry_id,material["occurred_at"],material["recorded_at"],material["source"],material["source_event_id"],material["pricing_version"],material["cost_usd"],payload_json,previous_hash,entry_hash))
                seq=cur.lastrowid; count+=1; head_seq=seq
                stored={"seq":seq,"entry_id":entry_id,"occurred_at":material["occurred_at"],"recorded_at":material["recorded_at"],"source":material["source"],"source_event_id":material["source_event_id"],"pricing_version":material["pricing_version"],"cost_usd":material["cost_usd"],"payload":payload,"previous_hash":previous_hash,"entry_hash":entry_hash}
                previous_hash=entry_hash; results.append((stored,True))
            if head_seq is not None:
                conn.execute("INSERT OR REPLACE INTO spend_ledger_meta(id,count,head_seq,head_hash,updated_at) VALUES(1,?,?,?,?)",(count,head_seq,previous_hash,self.now_iso()))
            conn.execute("COMMIT")
            return results
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally:
            conn.close()
    def ingest_events_with_ledger(self,events,facts):
        """Atomically write AI events and their spend-ledger facts.

        Events are stored first-write-wins on ``event_id`` (ON CONFLICT DO
        NOTHING), matching the ledger's dedup-by-``entry_id`` semantics, so a
        resubmitted event id can never mutate a fact the ledger already
        recorded. Both writes run in one ``BEGIN IMMEDIATE`` transaction: if
        the ledger append fails, the event rows are rolled back too, so
        ``ai_events`` and the ledger cannot disagree after a successful call.

        Returns per-table write counts.
        """
        event_list=list(events); fact_list=list(facts)
        conn=self._open_ledger_conn()
        inserted=dup_events=created=duplicates=0
        try:
            conn.execute("BEGIN IMMEDIATE")
            last=conn.execute("SELECT entry_hash FROM spend_ledger ORDER BY seq DESC LIMIT 1").fetchone()
            previous_hash=last["entry_hash"] if last is not None else GENESIS_HASH
            meta=conn.execute("SELECT count,head_seq FROM spend_ledger_meta WHERE id=1").fetchone()
            count=meta["count"] if meta is not None else 0; head_seq=meta["head_seq"] if meta is not None else None
            for event,fact in zip(event_list,fact_list):
                d=self._event_row_params(event); cols=list(d.keys())
                cur=conn.execute(f"INSERT INTO ai_events({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) ON CONFLICT(event_id) DO NOTHING",[d[c] for c in cols])
                inserted+= 1 if cur.rowcount==1 else 0; dup_events+= 1 if cur.rowcount!=1 else 0
                entry_id=fact["entry_id"]; payload=fact.get("payload") or {}
                payload_json=json.dumps(payload,sort_keys=True,default=str)
                existing=conn.execute("SELECT entry_id FROM spend_ledger WHERE entry_id=?",(entry_id,)).fetchone()
                if existing is not None:
                    duplicates+=1; continue
                material={"entry_id":entry_id,"occurred_at":fact.get("occurred_at"),"recorded_at":fact.get("recorded_at"),"source":fact.get("source"),"source_event_id":fact.get("source_event_id"),"pricing_version":fact.get("pricing_version"),"cost_usd":float(fact.get("cost_usd") or 0.0),"payload":payload}
                entry_hash=compute_entry_hash(previous_hash,material)
                row=conn.execute("INSERT INTO spend_ledger(entry_id,occurred_at,recorded_at,source,source_event_id,pricing_version,cost_usd,payload_json,previous_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",(entry_id,material["occurred_at"],material["recorded_at"],material["source"],material["source_event_id"],material["pricing_version"],material["cost_usd"],payload_json,previous_hash,entry_hash))
                head_seq=row.lastrowid; count+=1; created+=1; previous_hash=entry_hash
            if head_seq is not None:
                conn.execute("INSERT OR REPLACE INTO spend_ledger_meta(id,count,head_seq,head_hash,updated_at) VALUES(1,?,?,?,?)",(count,head_seq,previous_hash,self.now_iso()))
            conn.execute("COMMIT")
            return {"events_inserted":inserted,"events_duplicate":dup_events,"ledger_created":created,"ledger_duplicates":duplicates}
        except Exception:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        finally:
            conn.close()
    def _ledger_row(self,r):
        d=dict(r); d["payload"]=json.loads(d.pop("payload_json") or "{}"); return d
    def ledger_entries(self,limit=100):
        rows=self.conn.execute("SELECT * FROM spend_ledger ORDER BY seq DESC LIMIT ?",(int(limit),)).fetchall(); return [self._ledger_row(r) for r in rows]
    def ledger_entries_all(self):
        rows=self.conn.execute("SELECT * FROM spend_ledger ORDER BY seq ASC").fetchall(); return [self._ledger_row(r) for r in rows]
    def ledger_entry(self,entry_id):
        r=self.conn.execute("SELECT * FROM spend_ledger WHERE entry_id=?",(entry_id,)).fetchone(); return self._ledger_row(r) if r is not None else None
    def ledger_count(self):
        return int(self.conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0] or 0)
    def ledger_meta(self):
        r=self.conn.execute("SELECT * FROM spend_ledger_meta WHERE id=1").fetchone(); return dict(r) if r is not None else None
    def _event_row_params(self,e):
        d=dict(e); d["policy_reasons_json"]=json.dumps(d.pop("policy_reasons",[])); d["metadata_json"]=json.dumps(d.pop("metadata",{})); d["success"]=int(bool(d["success"])); d["task_success"]=None if d.get("task_success") is None else int(bool(d["task_success"])); d["timestamp"]=d["timestamp"].isoformat() if hasattr(d["timestamp"],"isoformat") else str(d["timestamp"]); return d
    def insert_event(self,e):
        # First-write-wins on event_id: a resubmitted event must not silently
        # mutate a fact the spend ledger has already recorded (cost drift).
        d=self._event_row_params(e); cols=list(d.keys())
        self.conn.execute(f"INSERT INTO ai_events({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) ON CONFLICT(event_id) DO NOTHING",[d[c] for c in cols]); self.conn.commit()
    def billable_events(self):
        return [{"event_id":r["event_id"],"estimated_cost_usd":float(r["estimated_cost_usd"] or 0.0)} for r in self.conn.execute("SELECT event_id,estimated_cost_usd FROM ai_events").fetchall()]
    def events(self,hours:int=24,limit:int=5000):
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(); rows=self.conn.execute("SELECT * FROM ai_events WHERE timestamp>=? ORDER BY timestamp DESC LIMIT ?",(cutoff,limit)).fetchall(); return [self._event_row(r) for r in rows]
    def _event_row(self,r):
        d=dict(r); d["policy_reasons"]=json.loads(d.pop("policy_reasons_json") or "[]"); d["metadata"]=json.loads(d.pop("metadata_json") or "{}"); d["success"]=bool(d["success"]); d["task_success"]=None if d["task_success"] is None else bool(d["task_success"]); return d
    def insert_workflow(self,w):
        d=dict(w); d["captured_at"]=d["captured_at"].isoformat() if hasattr(d["captured_at"],"isoformat") else str(d["captured_at"]); d["expected_json"]=json.dumps(d.pop("expected",{})); d["tool_trace_json"]=json.dumps(d.pop("tool_trace",[])); d["metadata_json"]=json.dumps(d.pop("metadata",{})); cols=list(d.keys()); self.conn.execute(f"INSERT OR REPLACE INTO workflows({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",[d[c] for c in cols]); self.conn.commit()
    def list_workflows(self,limit=100): return [self._workflow_row(r) for r in self.conn.execute("SELECT * FROM workflows ORDER BY captured_at DESC LIMIT ?",(limit,)).fetchall()]
    def get_workflow(self,wid):
        r=self.conn.execute("SELECT * FROM workflows WHERE workflow_id=?",(wid,)).fetchone(); return self._workflow_row(r) if r else None
    def _workflow_row(self,r):
        d=dict(r); d["expected"]=json.loads(d.pop("expected_json") or "{}"); d["tool_trace"]=json.loads(d.pop("tool_trace_json") or "[]"); d["metadata"]=json.loads(d.pop("metadata_json") or "{}"); return d
    def insert_replay(self,r):
        d=dict(r); d["task_success"]=int(bool(d["task_success"])); cols=list(d.keys()); self.conn.execute(f"INSERT OR REPLACE INTO replay_runs({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",[d[c] for c in cols]); self.conn.commit()
    def replay_runs(self,limit=1000):
        return [dict(r) | {"task_success":bool(r["task_success"])} for r in self.conn.execute("SELECT * FROM replay_runs ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]
    def upsert_policy(self,p):
        pid=p.get("policy_id") or f"pol_{uuid.uuid4().hex[:12]}"; now=self.now_iso(); self.conn.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?,?,?,?,?,?,?)",(pid,p["name"],p["mode"],p.get("priority",100),int(p.get("enabled",True)),json.dumps(p.get("scope",{})),json.dumps(p.get("rules",{})),p.get("created_at",now),now)); self.conn.commit(); return pid
    def list_policies(self):
        rows=self.conn.execute("SELECT * FROM policies ORDER BY priority,name").fetchall(); return [{**dict(r),"enabled":bool(r["enabled"]),"scope":json.loads(r["scope_json"]),"rules":json.loads(r["rules_json"])} for r in rows]
    def upsert_budget(self,b):
        bid=b.get("budget_id") or f"bud_{uuid.uuid4().hex[:12]}"; now=self.now_iso(); self.conn.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?,?,?,?,?,?)",(bid,b["scope_type"],b["scope_value"],b["monthly_limit_usd"],b.get("warning_threshold",.8),int(b.get("hard_limit",False)),b.get("overage_action","warn"),b.get("created_at",now),now)); self.conn.commit(); return bid
    def list_budgets(self): return [{**dict(r),"hard_limit":bool(r["hard_limit"])} for r in self.conn.execute("SELECT * FROM budgets ORDER BY scope_type,scope_value").fetchall()]
    def monthly_spend(self,scope_type,scope_value):
        field={"team":"team","application":"application","workflow":"workflow"}[scope_type]; cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat(); return float(self.conn.execute(f"SELECT COALESCE(SUM(estimated_cost_usd),0) FROM ai_events WHERE {field}=? AND timestamp>=?",(scope_value,cutoff)).fetchone()[0] or 0)
    def audit(self,kind,actor,subject,decision,details):
        aid=f"aud_{uuid.uuid4().hex[:12]}"; self.conn.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?)",(aid,self.now_iso(),kind,actor,subject,decision,json.dumps(details))); self.conn.commit(); return aid
    def audits(self,limit=100): return [{**dict(r),"details":json.loads(r["details_json"] or "{}")} for r in self.conn.execute("SELECT * FROM audit ORDER BY timestamp DESC LIMIT ?",(limit,)).fetchall()]

    # -- demo data scoping -------------------------------------------------
    # Demo reset must never delete records the operator did not create as
    # demo. Seed registers every row it writes here; clear_demo deletes only
    # those registered rows. Real telemetry, policies and budgets are never
    # touched. The spend ledger is never touched (see below).
    _DEMO_TABLES=(("ai_events","event_id"),("workflows","workflow_id"),("replay_runs","replay_id"),("policies","policy_id"),("budgets","budget_id"),("audit","audit_id"))
    def register_demo_records(self,table,ids):
        rows=[(table,str(i)) for i in ids if i]
        if rows:
            self.conn.executemany("INSERT OR IGNORE INTO demo_records(table_name,record_id) VALUES(?,?)",rows); self.conn.commit()
    def clear_demo(self):
        # Deletes ONLY rows previously registered as demo, per table. Retained
        # spend-ledger entries are intentionally left in place: wiping them
        # would destroy history and mask deletion by removing the head record.
        for table,col in self._DEMO_TABLES:
            ids=[r[0] for r in self.conn.execute("SELECT record_id FROM demo_records WHERE table_name=?",(table,)).fetchall()]
            for i in range(0,len(ids),400):
                chunk=ids[i:i+400]; ph=",".join("?" for _ in chunk)
                self.conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ph})",chunk)
        self.conn.execute("DELETE FROM demo_records")
        self.conn.commit()

class ClickHouseRepository(Repository):
    backend="clickhouse"
    def __init__(self,url,database="aiops",user="default",password=""):
        self.url=url.rstrip("/"); self.database=database; self.auth=(user,password) if user else None; self.client=httpx.Client(timeout=15)
        # Bootstrap the database through the default namespace before querying it.
        self.client.post(self.url,params={"query":f"CREATE DATABASE IF NOT EXISTS {self.database}"},auth=self.auth).raise_for_status()
        self.init_schema(); self._execute("SELECT 1")
    def _execute(self,sql,params=None):
        q=sql if params is None else sql.format(**{k:self._quote(v) for k,v in params.items()}); r=self.client.post(self.url,params={"database":self.database if not sql.startswith("CREATE DATABASE") else "default"},content=q.encode(),auth=self.auth); r.raise_for_status(); return r.text
    def _quote(self,v): return "'"+str(v).replace("\\","\\\\").replace("'","\\'")+"'"
    def _insert_json(self,table,row):
        r=self.client.post(self.url,params={"database":self.database,"date_time_input_format":"best_effort","query":f"INSERT INTO {table} FORMAT JSONEachRow"},content=(json.dumps(row,default=str)+"\n").encode(),auth=self.auth); r.raise_for_status()
    def _query_json(self,sql):
        r=self.client.post(self.url,params={"database":self.database,"query":sql+" FORMAT JSONEachRow"},auth=self.auth); r.raise_for_status(); return [json.loads(x) for x in r.text.splitlines() if x.strip()]
    def init_schema(self):
        self.client.post(self.url,params={"query":f"CREATE DATABASE IF NOT EXISTS {self.database}"},auth=self.auth).raise_for_status()
        stmts=[
        """CREATE TABLE IF NOT EXISTS ai_events(event_id String,timestamp DateTime64(3),request_id String,trace_id String,session_id Nullable(String),user_id LowCardinality(String),team LowCardinality(String),application LowCardinality(String),workflow LowCardinality(String),agent LowCardinality(String),provider LowCardinality(String),model LowCardinality(String),input_tokens UInt64,output_tokens UInt64,cached_tokens UInt64,latency_ms UInt64,estimated_cost_usd Float64,success UInt8,error_code String,tool_calls UInt32,retry_count UInt32,data_classification LowCardinality(String),policy_decision LowCardinality(String),policy_reasons_json String,task_success Nullable(UInt8),quality_score Nullable(Float64),metadata_json String) ENGINE=MergeTree ORDER BY (timestamp,team,application,workflow,event_id)""",
        """CREATE TABLE IF NOT EXISTS workflows(workflow_id String,captured_at DateTime64(3),name String,prompt String,context String,baseline_provider String,baseline_model String,baseline_output String,baseline_input_tokens UInt64,baseline_output_tokens UInt64,baseline_cost_usd Float64,expected_json String,tool_trace_json String,metadata_json String) ENGINE=ReplacingMergeTree ORDER BY workflow_id""",
        """CREATE TABLE IF NOT EXISTS replay_runs(replay_id String,workflow_id String,candidate_provider String,candidate_model String,estimated_cost_usd Float64,latency_ms UInt64,task_success UInt8,quality_score Float64,output String,created_at DateTime64(3)) ENGINE=MergeTree ORDER BY (workflow_id,created_at,replay_id)""",
        """CREATE TABLE IF NOT EXISTS policies(policy_id String,name String,mode String,priority Int32,enabled UInt8,scope_json String,rules_json String,created_at DateTime64(3),updated_at DateTime64(3)) ENGINE=ReplacingMergeTree(updated_at) ORDER BY policy_id""",
        """CREATE TABLE IF NOT EXISTS budgets(budget_id String,scope_type String,scope_value String,monthly_limit_usd Float64,warning_threshold Float64,hard_limit UInt8,overage_action String,created_at DateTime64(3),updated_at DateTime64(3)) ENGINE=ReplacingMergeTree(updated_at) ORDER BY budget_id""",
        """CREATE TABLE IF NOT EXISTS audit(audit_id String,timestamp DateTime64(3),kind String,actor String,subject String,decision String,details_json String) ENGINE=MergeTree ORDER BY (timestamp,audit_id)""" ]
        for s in stmts:self._execute(s)
    def insert_event(self,e):
        d=dict(e); d["timestamp"]=d["timestamp"].isoformat() if hasattr(d["timestamp"],"isoformat") else str(d["timestamp"]); d["policy_reasons_json"]=json.dumps(d.pop("policy_reasons",[])); d["metadata_json"]=json.dumps(d.pop("metadata",{})); d["success"]=int(bool(d["success"])); d["task_success"]=None if d.get("task_success") is None else int(bool(d["task_success"])); self._insert_json("ai_events",d)
    def events(self,hours=24,limit=5000):
        rows=self._query_json(f"SELECT * FROM ai_events WHERE timestamp >= now() - INTERVAL {int(hours)} HOUR ORDER BY timestamp DESC LIMIT {int(limit)}");
        for d in rows: d["policy_reasons"]=json.loads(d.pop("policy_reasons_json") or "[]"); d["metadata"]=json.loads(d.pop("metadata_json") or "{}"); d["success"]=bool(d["success"]); d["task_success"]=None if d.get("task_success") is None else bool(d["task_success"])
        return rows
    def insert_workflow(self,w):
        d=dict(w); d["captured_at"]=d["captured_at"].isoformat() if hasattr(d["captured_at"],"isoformat") else str(d["captured_at"]); d["expected_json"]=json.dumps(d.pop("expected",{})); d["tool_trace_json"]=json.dumps(d.pop("tool_trace",[])); d["metadata_json"]=json.dumps(d.pop("metadata",{})); self._insert_json("workflows",d)
    def list_workflows(self,limit=100):
        rows=self._query_json(f"SELECT * FROM workflows FINAL ORDER BY captured_at DESC LIMIT {int(limit)}"); return [self._workflow_row(d) for d in rows]
    def get_workflow(self,wid):
        rows=self._query_json(f"SELECT * FROM workflows FINAL WHERE workflow_id={self._quote(wid)} ORDER BY captured_at DESC LIMIT 1"); return self._workflow_row(rows[0]) if rows else None
    def _workflow_row(self,d): d=dict(d); d["expected"]=json.loads(d.pop("expected_json") or "{}"); d["tool_trace"]=json.loads(d.pop("tool_trace_json") or "[]"); d["metadata"]=json.loads(d.pop("metadata_json") or "{}"); return d
    def insert_replay(self,r): d=dict(r); d["task_success"]=int(bool(d["task_success"])); self._insert_json("replay_runs",d)
    def replay_runs(self,limit=1000): return [{**r,"task_success":bool(r["task_success"])} for r in self._query_json(f"SELECT * FROM replay_runs ORDER BY created_at DESC LIMIT {int(limit)}")]
    def upsert_policy(self,p):
        pid=p.get("policy_id") or f"pol_{uuid.uuid4().hex[:12]}"; now=self.now_iso(); self._insert_json("policies",{"policy_id":pid,"name":p["name"],"mode":p["mode"],"priority":p.get("priority",100),"enabled":int(p.get("enabled",True)),"scope_json":json.dumps(p.get("scope",{})),"rules_json":json.dumps(p.get("rules",{})),"created_at":p.get("created_at",now),"updated_at":now}); return pid
    def list_policies(self):
        rows=self._query_json("SELECT * FROM policies FINAL ORDER BY priority,name"); return [{**r,"enabled":bool(r["enabled"]),"scope":json.loads(r["scope_json"]),"rules":json.loads(r["rules_json"])} for r in rows]
    def upsert_budget(self,b):
        bid=b.get("budget_id") or f"bud_{uuid.uuid4().hex[:12]}"; now=self.now_iso(); self._insert_json("budgets",{"budget_id":bid,"scope_type":b["scope_type"],"scope_value":b["scope_value"],"monthly_limit_usd":b["monthly_limit_usd"],"warning_threshold":b.get("warning_threshold",.8),"hard_limit":int(b.get("hard_limit",False)),"overage_action":b.get("overage_action","warn"),"created_at":b.get("created_at",now),"updated_at":now}); return bid
    def list_budgets(self): return [{**r,"hard_limit":bool(r["hard_limit"])} for r in self._query_json("SELECT * FROM budgets FINAL ORDER BY scope_type,scope_value")]
    def monthly_spend(self,scope_type,scope_value):
        field={"team":"team","application":"application","workflow":"workflow"}[scope_type]; rows=self._query_json(f"SELECT sum(estimated_cost_usd) AS spend FROM ai_events WHERE {field}={self._quote(scope_value)} AND timestamp >= now()-INTERVAL 30 DAY"); return float(rows[0].get("spend") or 0) if rows else 0.0
    def audit(self,kind,actor,subject,decision,details): self._insert_json("audit",{"audit_id":f"aud_{uuid.uuid4().hex[:12]}","timestamp":self.now_iso(),"kind":kind,"actor":actor,"subject":subject,"decision":decision,"details_json":json.dumps(details)})
    def audits(self,limit=100): return [{**r,"details":json.loads(r["details_json"] or "{}")} for r in self._query_json(f"SELECT * FROM audit ORDER BY timestamp DESC LIMIT {int(limit)}")]
    def clear_demo(self):
        # Refuse rather than TRUNCATE: on ClickHouse demo rows are not tracked,
        # so a wholesale truncate would delete real telemetry too. Demo seeding
        # skips clearing on this backend (see supports_demo_scoping). A future
        # demo-scoped delete must be designed and tested first.
        raise NotImplementedError("Demo-scoped clearing is not implemented for ClickHouse; see docs/LEDGER_DESIGN.md")
    # -- spend ledger (not implemented on ClickHouse) ----------------------
    # ClickHouse does not provide multi-statement row-level transactions, so a
    # hash chain cannot be written atomically through it. The supported design
    # is to keep the ledger in a transactional single-writer store and use
    # ClickHouse only as an analytical projection. See docs/LEDGER_DESIGN.md.
    supports_ledger=False
    _LEDGER_UNSUPPORTED="Spend ledger is not implemented for ClickHouse; see docs/LEDGER_DESIGN.md"
    def append_ledger(self,fact): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def append_ledger_many(self,facts): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def ledger_entries(self,limit=100): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def ledger_entries_all(self): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def ledger_entry(self,entry_id): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def ledger_count(self): raise NotImplementedError(self._LEDGER_UNSUPPORTED)
    def ledger_meta(self): raise NotImplementedError(self._LEDGER_UNSUPPORTED)

def create_repository(settings):
    if settings.storage=="clickhouse":
        try: return ClickHouseRepository(settings.clickhouse_url,settings.clickhouse_database,settings.clickhouse_user,settings.clickhouse_password)
        except Exception:
            if not settings.allow_storage_fallback: raise
    return SQLiteRepository(settings.sqlite_path)
