#!/usr/bin/env python3
"""Import drained v4 delivery state into a separate task-container deployment.

Dry-run by default. Never changes the source, talks to Docker, or contacts a relay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_ci.protocol import ContractError, RESULT_SCHEMA, atomic_json, canonical, validate_task
from agent_ci.state import Journal

SCHEMA = "triton-anchor-task-container-migration/v1"
TERMINAL = {"complete", "cancelled", "incomplete"}


def safe_root(value: Path) -> Path:
    path = Path(value).absolute()
    if path.resolve() != path or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ContractError("Migration roots must not contain symlinks")
    return path


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def saved_files(root: Path):
    if not root.is_dir() or root.is_symlink():
        raise ContractError("Sealed evidence directory is unavailable")
    result = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            if path.is_symlink() or os.path.ismount(path):
                raise ContractError("Evidence may not contain symlinks or nested mounts")
            if path.is_dir():
                continue
            if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 64 * 1024**2:
                raise ContractError("Evidence must contain bounded regular files without hardlinks")
            result.append((path, sha256(path)))
            if len(result) > 100000:
                raise ContractError("Too many evidence files")
    return result


def inspect(source: Path, target: Path, inventory: dict) -> tuple[dict, list]:
    source, target = safe_root(source), safe_root(target)
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ContractError("Source and target state directories must be separate")
    required = ("old_intake_stopped", "old_worker_stopped", "old_containers_stopped", "leases_released")
    if not isinstance(inventory, dict) or any(inventory.get(k) is not True for k in required):
        raise ContractError("Migration requires confirmed stopped intake, worker, containers and released leases")
    entries = inventory.get("tasks", [])
    if not isinstance(entries, list) or any(not isinstance(r, dict) or not r.get("task_id") for r in entries):
        raise ContractError("Invalid drained task inventory")
    by_task = {r["task_id"]: r for r in entries}
    if len(by_task) != len(entries):
        raise ContractError("Duplicate task in migration inventory")
    database = source / "journal.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ContractError("Source journal is missing")
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ContractError("Checkpoint the stopped source journal before taking the migration snapshot")
    prepared = []
    with sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY task_id")]
        boxes = {r["task_id"]: dict(r) for r in db.execute("SELECT * FROM outbox")}
    for row in rows:
        task = validate_task(json.loads(row["manifest"]), ("likehupochuan/triton-anchor", "anteloper-c/triton-anchor"))
        if task["task_id"] != row["task_id"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", row["run_id"]):
            raise ContractError("Source task/run identity is invalid")
        evidence, box = [], boxes.get(row["task_id"])
        phase, reason = row["phase"], "migrated_terminal_state"
        if box:
            source_file = Path(box["payload_path"])
            if (not source_file.is_absolute() or source_file.resolve() != source_file
                    or not source_file.is_relative_to(source) or not source_file.is_file()
                    or sha256(source_file) != box["digest"]):
                raise ContractError("Source outbox digest/path is invalid")
            payload = json.loads(source_file.read_bytes())
            if payload.get("schema") != RESULT_SCHEMA or payload.get("task") != task or payload.get("run_id") != row["run_id"]:
                raise ContractError("Sealed result identity differs from source journal")
            destination = target / "tasks" / row["task_id"] / row["run_id"] / "published"
            evidence = [(path, destination / path.relative_to(source_file.parent), digest)
                        for path, digest in saved_files(source_file.parent)]
            box = {k: box[k] for k in ("task_id", "digest", "attempts", "published")}
            box["payload_path"] = str(destination / source_file.name)
            phase = "complete" if box["published"] is not None else "publishing"
            reason = "migrated_upload" if box["published"] is not None else "migrated_pending_upload"
        elif phase not in TERMINAL:
            declared = by_task.get(row["task_id"], {})
            if declared.get("state") != "cancelled" or not str(declared.get("reason", "")).strip():
                raise ContractError("Unsealed active work must be explicitly cancelled in the drained inventory")
            phase, reason = "cancelled", declared["reason"]
        item = {"row": {**row, "phase": phase, "detail": canonical({"reason": reason,
                    "runtime_migration": "task-containers", "old_execution_state_reused": False}).decode()},
                "outbox": box, "files": evidence}
        prepared.append(item)
    signature = hashlib.sha256(canonical({"tasks": [r["row"] for r in prepared], "outboxes": [r["outbox"] for r in prepared],
                                         "files": [(str(a.relative_to(source)), h) for r in prepared for a, _, h in r["files"]],
                                         "inventory": inventory})).hexdigest()
    report = {"schema": SCHEMA, "source_state": str(source), "target_state": str(target), "digest": signature,
              "tasks": len(prepared), "pending_uploads": sum(r["row"]["phase"] == "publishing" for r in prepared),
              "evidence_files": sum(len(r["files"]) for r in prepared), "old_execution_state_reused": False,
              "source_changed": False, "applied": False}
    return report, prepared


def migrate(source: Path, target: Path, inventory: dict, *, apply=False) -> dict:
    report, rows = inspect(source, target, inventory)
    if not apply:
        return report
    target = safe_root(target)
    target.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (target / "poll.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("Stop the target worker before importing state") from exc
        return apply_state(target, report, rows)


def validate_existing_target(db, rows):
    """Allow an interrupted identical import, never merge another execution.

    The task/outbox insertion is transactional, so a preexisting task without
    its expected outbox cannot be a partially committed import. A completed
    identical upload is allowed: retrying migration must not reopen delivery.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for item in rows:
        expected, box = item["row"], item["outbox"]
        task_id = expected["task_id"]
        existing = db.execute("SELECT manifest,run_id,phase FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        actual = db.execute("SELECT payload_path,digest,published FROM outbox WHERE task_id=?", (task_id,)).fetchone()
        for table in ("executions", "reviews", "task_workspaces"):
            if table in tables and db.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
                raise ContractError("Target contains conflicting execution/review/workspace state")
        if not existing:
            if actual:
                raise ContractError("Target contains a conflicting orphan outbox")
            continue
        if tuple(existing[:2]) != (expected["manifest"], expected["run_id"]):
            raise ContractError("Target contains a conflicting task/run")
        if (box is None) != (actual is None):
            raise ContractError("Target contains a conflicting outbox presence")
        if box is None:
            if existing["phase"] != expected["phase"]:
                raise ContractError("Target contains a conflicting terminal task phase")
            continue
        if actual["payload_path"] != box["payload_path"] or actual["digest"] != box["digest"]:
            raise ContractError("Target contains a conflicting immutable outbox")
        path = Path(actual["payload_path"])
        if path.resolve() != path or not path.is_file() or sha256(path) != box["digest"]:
            raise ContractError("Target immutable outbox bytes differ")
        actual_phase = "complete" if actual["published"] is not None else "publishing"
        if existing["phase"] != actual_phase or box["published"] is not None and actual["published"] is None:
            raise ContractError("Target contains a conflicting upload state")


def apply_state(target, report, rows):
    marker = target / "task-container-migration.json"
    if marker.exists():
        previous = json.loads(marker.read_bytes())
        if previous.get("digest") != report["digest"]:
            raise ContractError("Target already has a different migration")
        for item in rows:
            for _, dest, digest in item["files"]:
                if dest.resolve() != dest or not dest.is_file() or sha256(dest) != digest:
                    raise ContractError("Previously migrated evidence no longer matches")
        return {**previous, "already_applied": True}
    journal = Journal(target)
    with journal.connect() as db:
        validate_existing_target(db, rows)
    for item in rows:
        for source_file, dest, digest in item["files"]:
            if dest.resolve() != dest:
                raise ContractError("Target evidence path contains a symlink")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if not dest.is_file() or sha256(dest) != digest:
                    raise ContractError("Target immutable evidence differs")
                continue
            temp = dest.with_name("." + dest.name + ".migrating")
            if temp.is_symlink():
                raise ContractError("Unsafe migration staging path")
            shutil.copyfile(source_file, temp)
            if sha256(temp) != digest:
                raise ContractError("Source evidence changed during migration")
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, dest)
            fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    with journal.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        validate_existing_target(db, rows)
        for item in rows:
            row, box = item["row"], item["outbox"]
            db.execute("INSERT OR IGNORE INTO tasks(task_id,subject,manifest,run_id,phase,updated,detail) VALUES(?,?,?,?,?,?,?)",
                       tuple(row[k] for k in ("task_id", "subject", "manifest", "run_id", "phase", "updated", "detail")))
            if box:
                db.execute("INSERT OR IGNORE INTO outbox(task_id,payload_path,digest,attempts,published) VALUES(?,?,?,?,?)",
                           tuple(box[k] for k in ("task_id", "payload_path", "digest", "attempts", "published")))
            db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                       (row["task_id"], time.time(), "runtime_migrated", row["detail"]))
    report.update(applied=True, completed_at=time.time())
    atomic_json(marker, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-state", required=True, type=Path)
    parser.add_argument("--target-state", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.source_state, args.target_state, json.loads(args.inventory.read_bytes()), apply=args.apply), indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("State migration failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
