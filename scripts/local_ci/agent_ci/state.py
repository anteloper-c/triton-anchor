"""Durable task journal. Only the trusted worker writes this database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .protocol import ContractError, canonical, current_key


class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "journal.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                  task_id TEXT PRIMARY KEY, subject TEXT NOT NULL, manifest TEXT NOT NULL,
                  run_id TEXT NOT NULL, phase TEXT NOT NULL, updated REAL NOT NULL,
                  detail TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS executions (
                  execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_id TEXT NOT NULL,
                  variant TEXT NOT NULL, status TEXT NOT NULL, record TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                  task_id TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL,
                  PRIMARY KEY(task_id,kind));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at REAL NOT NULL,
                  kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                  task_id TEXT PRIMARY KEY, payload_path TEXT NOT NULL, digest TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, published REAL);
            """)
        self.path.chmod(0o600)

    @staticmethod
    def result_status(box: dict) -> str | None:
        """Read the sealed outcome; delivery completion is not a green result."""
        try:
            data = Path(box["payload_path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != box["digest"]:
                return None
            document = json.loads(data)
            return document.get("status") if isinstance(document, dict) else None
        except (OSError, ValueError):
            return None

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def register(self, task: dict) -> dict:
        with self.connect() as db:
            existing = db.execute("SELECT * FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
            if existing:
                if json.loads(existing["manifest"]) != task:
                    raise ContractError("Immutable task manifest changed")
                return dict(existing)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", (
                task["task_id"], current_key(task), canonical(task).decode(), run_id,
                "queued", time.time(), "{}"))
        self.event(task["task_id"], "registered", {"run_id": run_id})
        return self.task(task["task_id"])

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ContractError("Unknown task")
        return dict(row)

    def tasks(self, *, active: bool = True) -> list[dict]:
        with self.connect() as db:
            query = "SELECT * FROM tasks"
            if active:
                query += " WHERE phase NOT IN ('complete','cancelled','incomplete')"
            return [dict(row) for row in db.execute(query + " ORDER BY updated")]

    def phase(self, task_id: str, phase: str, detail: dict | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET phase=?, updated=?, detail=? WHERE task_id=?",
                       (phase, time.time(), canonical(detail or {}).decode(), task_id))
        self.event(task_id, "phase:" + phase, detail or {})

    def event(self, task_id: str, kind: str, detail: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                       (task_id, time.time(), kind, canonical(detail).decode()))

    def execution(self, task_id: str, tool_id: str, variant: str, record: dict) -> str:
        ident = record.get("execution_id") or uuid.uuid4().hex
        record = {**record, "execution_id": ident, "tool_id": tool_id, "variant": variant}
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status,record=excluded.record,updated=excluded.updated",
                       (ident, task_id, tool_id, variant, record["status"], canonical(record).decode(), now, now))
        return ident

    def executions(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT record FROM executions WHERE task_id=? ORDER BY created", (task_id,))]

    def latest(self, task_id: str, tool_id: str, variant: str = "candidate") -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT record FROM executions WHERE task_id=? AND tool_id=? AND variant=? ORDER BY created DESC LIMIT 1",
                             (task_id, tool_id, variant)).fetchone()
        return json.loads(row[0]) if row else None

    def invalidate_workspace(self, task_id: str, reason: str, *, generation: str | None = None) -> None:
        """Retain execution facts, but forbid reusing removed installation state."""
        with self.connect() as db:
            for row in db.execute("SELECT execution_id,record FROM executions WHERE task_id=?", (task_id,)).fetchall():
                record = json.loads(row["record"])
                if generation is not None and record.get("workspace_generation", generation) != generation:
                    continue
                record["reuse_invalidated"] = reason
                db.execute("UPDATE executions SET record=?,updated=? WHERE execution_id=?",
                           (canonical(record).decode(), time.time(), row["execution_id"]))
        self.event(task_id, "workspace:checks_invalidated", {"reason": reason, "generation": generation})

    def review(self, task_id: str, kind: str, record: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?)", (task_id, kind, canonical(record).decode()))
        self.event(task_id, "review:" + kind, record)

    def reviews(self, task_id: str) -> dict:
        with self.connect() as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT kind,record FROM reviews WHERE task_id=?", (task_id,))}

    def queue_result(self, task_id: str, path: Path, result_digest: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT digest,published FROM outbox WHERE task_id=?", (task_id,)).fetchone()
            if row and row["digest"] != result_digest:
                raise ContractError("A sealed result cannot be rewritten; resume explicitly before republishing")
            if row and row["published"] is not None:
                return
            db.execute("INSERT OR IGNORE INTO outbox(task_id,payload_path,digest) VALUES(?,?,?)",
                       (task_id, str(path), result_digest))
            db.execute("UPDATE tasks SET phase='publishing',updated=?,detail='{}' WHERE task_id=?", (time.time(), task_id))
        self.event(task_id, "phase:publishing", {})

    def outbox(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM outbox WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def published(self, task_id: str) -> None:
        with self.connect() as db:
            box = db.execute("SELECT * FROM outbox WHERE task_id=?", (task_id,)).fetchone()
            if not box:
                raise ContractError("Cannot complete delivery without a sealed outbox")
            now = time.time()
            uploaded_at = box["published"] if box["published"] is not None else now
            detail = {"completion_boundary": "gitee_upload", "uploaded_at": uploaded_at,
                      "result_status": self.result_status(dict(box))}
            if box["published"] is None:
                db.execute("UPDATE outbox SET attempts=attempts+1,published=? WHERE task_id=?", (uploaded_at, task_id))
            db.execute("UPDATE tasks SET phase='complete',updated=?,detail=? WHERE task_id=?", (now, canonical(detail).decode(), task_id))
            db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)", (task_id, now, "gitee_upload_complete", canonical(detail).decode()))

    def publication_failure(self, task_id: str) -> int:
        with self.connect() as db:
            db.execute("UPDATE outbox SET attempts=attempts+1 WHERE task_id=?", (task_id,))
            return db.execute("SELECT attempts FROM outbox WHERE task_id=?", (task_id,)).fetchone()[0]

    def resume(self, task_id: str) -> None:
        row = self.task(task_id)
        box = self.outbox(task_id)
        if box and box["published"] is None and row["phase"] in {"publishing", "incomplete"}:
            self.phase(task_id, "publishing", {"resumed": True, "reason": "retry_saved_upload"})
            return
        if row["phase"] not in {"incomplete", "complete"} or (row["phase"] == "complete" and (not box or self.result_status(box) != "infra_error")):
            raise ContractError("Only unfinished infrastructure results or pending uploads can be explicitly resumed")
        with self.connect() as db:
            # Preserve evidence; resumed tests use a new immutable run identity.
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
            db.execute("UPDATE tasks SET run_id=?,phase='queued',updated=?,detail='{}' WHERE task_id=?", (run_id, time.time(), task_id))
            db.execute("DELETE FROM outbox WHERE task_id=?", (task_id,))
        self.event(task_id, "explicit_resume", {"previous_run_id": row["run_id"], "run_id": run_id})
