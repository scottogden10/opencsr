"""SQLite ledger — the system of record for the clinical control plane.

Append-only audit trail, task/evidence/claim/proposal/issue/feedback state,
tool receipts, eval history, and the skill-version registry. Provider
session state (Managed Agents sessions) is disposable; this database is not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path

from .domain import now_iso, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, task_type TEXT, title TEXT, status TEXT,
  target_node TEXT, snapshot TEXT, config TEXT, result TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task_id TEXT, agent TEXT, agent_version TEXT,
  backend TEXT, status TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, task_id TEXT, locator TEXT, artifact TEXT,
  artifact_hash TEXT, fragment TEXT, role TEXT, created_by TEXT,
  verified INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY, task_id TEXT, claim_type TEXT, text TEXT,
  dimensions TEXT, value TEXT, support_class TEXT, evidence_ids TEXT,
  status TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, task_id TEXT, target_node TEXT, base_version INTEGER,
  base_hash TEXT, text TEXT, sentence_map TEXT, status TEXT,
  revision INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS issues (
  id TEXT PRIMARY KEY, task_id TEXT, category TEXT, severity TEXT,
  description TEXT, refs TEXT, raised_by TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, task_id TEXT, proposal_id TEXT, decision TEXT,
  reason_code TEXT, scope TEXT, comment TEXT, final_text TEXT, actor TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, tool TEXT, tool_input TEXT,
  output_digest TEXT, allowed INTEGER, error TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, event_type TEXT,
  task_id TEXT, actor_chain TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  node TEXT, version INTEGER, text TEXT, text_hash TEXT, updated_at TEXT,
  updated_by TEXT, task_id TEXT, PRIMARY KEY (node, version));
CREATE TABLE IF NOT EXISTS evals (
  id TEXT PRIMARY KEY, kind TEXT, label TEXT, config TEXT, metrics TEXT,
  hard_gates_passed INTEGER, score REAL, report TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS skill_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, skill TEXT, version INTEGER,
  action TEXT, content_hash TEXT, hypothesis TEXT, eval_id TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- low-level helpers -------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()):  # noqa: ANN001
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _load_json_fields(row: dict, fields: tuple) -> dict:
        for f in fields:
            if row.get(f):
                try:
                    row[f] = json.loads(row[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        return row

    # -- audit (append-only) ----------------------------------------------
    def audit(self, event_type: str, task_id: str | None, actor_chain: dict,
              payload: dict) -> str:
        eid = new_id("ae")
        self._exec(
            "INSERT INTO audit (id, event_type, task_id, actor_chain, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (eid, event_type, task_id, json.dumps(actor_chain),
             json.dumps(payload, ensure_ascii=False), now_iso()))
        return eid

    def audit_events(self, task_id: str | None = None, limit: int = 500) -> list[dict]:
        if task_id:
            rows = self._rows(
                "SELECT * FROM audit WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, limit))
        else:
            rows = self._rows("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        return [self._load_json_fields(r, ("actor_chain", "payload")) for r in rows]

    # -- tasks -------------------------------------------------------------
    def create_task(self, task_type: str, title: str, target_node: str,
                    snapshot: dict, config: dict) -> str:
        tid = new_id("task")
        ts = now_iso()
        self._exec(
            "INSERT INTO tasks (id, task_type, title, status, target_node, snapshot,"
            " config, result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, task_type, title, "created", target_node, json.dumps(snapshot),
             json.dumps(config), "{}", ts, ts))
        return tid

    def update_task(self, task_id: str, status: str | None = None,
                    result: dict | None = None):
        if status is not None:
            self._exec("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                       (status, now_iso(), task_id))
        if result is not None:
            self._exec("UPDATE tasks SET result=?, updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), now_iso(), task_id))

    def get_task(self, task_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            return None
        return self._load_json_fields(rows[0], ("snapshot", "config", "result"))

    def list_tasks(self, limit: int = 100) -> list[dict]:
        rows = self._rows("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._load_json_fields(r, ("snapshot", "config", "result")) for r in rows]

    # -- generic dataclass writers ----------------------------------------
    def add(self, table: str, obj) -> None:  # noqa: ANN001
        d = asdict(obj)
        json_fields = {
            "evidence": ("fragment",),
            "claims": ("dimensions", "value", "evidence_ids"),
            "proposals": ("sentence_map",),
            "issues": ("refs",),
            "receipts": ("tool_input",),
            "runs": ("detail",),
        }.get(table, ())
        col_map = {"tool_input": "tool_input", "fragment": "fragment"}
        cols, vals = [], []
        for k, v in d.items():
            cols.append(col_map.get(k, k))
            if k in json_fields or isinstance(v, (dict, list)):
                vals.append(json.dumps(v, ensure_ascii=False))
            elif isinstance(v, bool):
                vals.append(1 if v else 0)
            else:
                vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        self._exec(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                   tuple(vals))

    def by_task(self, table: str, task_id: str) -> list[dict]:
        json_fields = {
            "evidence": ("fragment",),
            "claims": ("dimensions", "value", "evidence_ids"),
            "proposals": ("sentence_map",),
            "issues": ("refs",),
            "receipts": ("tool_input",),
            "runs": ("detail",),
            "feedback": (),
        }.get(table, ())
        rows = self._rows(f"SELECT * FROM {table} WHERE task_id=? ORDER BY created_at",
                          (task_id,))
        return [self._load_json_fields(r, json_fields) for r in rows]

    def get_row(self, table: str, row_id: str) -> dict | None:
        rows = self._rows(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        if not rows:
            return None
        json_fields = {
            "claims": ("dimensions", "value", "evidence_ids"),
            "proposals": ("sentence_map",),
            "issues": ("refs",),
            "runs": ("detail",),
        }.get(table, ())
        return self._load_json_fields(rows[0], json_fields)

    def update_fields(self, table: str, row_id: str, **fields):
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        vals.append(row_id)
        self._exec(f"UPDATE {table} SET {','.join(sets)} WHERE id=?", tuple(vals))

    # -- documents ---------------------------------------------------------
    def latest_document(self, node: str) -> dict | None:
        rows = self._rows(
            "SELECT * FROM documents WHERE node=? ORDER BY version DESC LIMIT 1", (node,))
        return rows[0] if rows else None

    def write_document(self, node: str, text: str, text_hash: str,
                       updated_by: str, task_id: str) -> int:
        latest = self.latest_document(node)
        version = (latest["version"] + 1) if latest else 1
        self._exec(
            "INSERT INTO documents (node, version, text, text_hash, updated_at,"
            " updated_by, task_id) VALUES (?,?,?,?,?,?,?)",
            (node, version, text, text_hash, now_iso(), updated_by, task_id))
        return version

    def document_history(self, node: str) -> list[dict]:
        return self._rows("SELECT * FROM documents WHERE node=? ORDER BY version", (node,))

    # -- evals & skills ----------------------------------------------------
    def add_eval(self, kind: str, label: str, config: dict, metrics: dict,
                 hard_gates_passed: bool, score: float, report: dict) -> str:
        eid = new_id("eval")
        self._exec(
            "INSERT INTO evals (id, kind, label, config, metrics, hard_gates_passed,"
            " score, report, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, kind, label, json.dumps(config), json.dumps(metrics),
             1 if hard_gates_passed else 0, score,
             json.dumps(report, ensure_ascii=False), now_iso()))
        return eid

    def list_evals(self, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM evals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._load_json_fields(r, ("config", "metrics", "report")) for r in rows]

    def skill_event(self, skill: str, version: int, action: str,
                    content_hash: str, hypothesis: dict | None, eval_id: str | None):
        self._exec(
            "INSERT INTO skill_events (skill, version, action, content_hash,"
            " hypothesis, eval_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (skill, version, action, content_hash,
             json.dumps(hypothesis) if hypothesis else None, eval_id, now_iso()))

    def skill_history(self) -> list[dict]:
        rows = self._rows("SELECT * FROM skill_events ORDER BY seq")
        return [self._load_json_fields(r, ("hypothesis",)) for r in rows]

    def close(self):
        with self._lock:
            self._conn.close()

    # -- kv ----------------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def kv_set(self, key: str, value: str):
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))
