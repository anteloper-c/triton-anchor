"""Task-volume lifecycle; host evidence and delivery never depend on Docker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path

from .executor import resource_lock
from .protocol import ContractError, atomic_json, canonical


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.digest()


class TaskWorkspaces:
    def __init__(self, config, journal, manager, relay, executor_factory):
        self.config, self.journal, self.manager = config, journal, manager
        self.relay, self.executor_factory = relay, executor_factory
        self.state_dir = journal.root
        self.checked = {}
        self.recovered = False
        self.discovery_errors = []
        self.retention = config.get("task_workspace_retention_hours", 24)
        self.budget = config.get("task_workspace_max_bytes", 100 * 1024**3)
        if type(self.retention) not in (int, float) or not 0 <= self.retention <= 87600:
            raise ContractError("Invalid task_workspace_retention_hours")
        if type(self.budget) is not int or self.budget <= 0:
            raise ContractError("Invalid task_workspace_max_bytes")
        with journal.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_workspaces (
                task_id TEXT NOT NULL, generation TEXT NOT NULL, manifest TEXT NOT NULL,
                phase TEXT NOT NULL, updated REAL NOT NULL, retained_at REAL,
                reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(task_id,generation))""")

    def rows(self):
        with self.journal.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM task_workspaces ORDER BY updated")]

    def phase(self, task_id, generation, phase, reason=""):
        now = time.time()
        with self.journal.connect() as db:
            db.execute("""UPDATE task_workspaces SET phase=?,updated=?,reason=?,
                retained_at=CASE WHEN ?='active' THEN NULL ELSE COALESCE(retained_at,?) END
                WHERE task_id=? AND generation=?""", (phase, now, reason, phase, now, task_id, generation))
        self.journal.event(task_id, "workspace:" + phase, {"attempt_id": generation, "reason": reason})

    def root(self, task_id, handle):
        attempt = handle.get("attempt_id", "")
        if not re.fullmatch(r"[a-f0-9]{64}", task_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", attempt):
            raise ContractError("Legacy persistent workspaces require offline migration")
        root = self.state_dir / "task-staging" / task_id / attempt
        if root.resolve() != root or any(p.is_symlink() for p in (root, *root.parents)):
            raise ContractError("Task staging path contains a symlink")
        if root.exists() and not root.is_dir():
            raise ContractError("Task staging path is not a directory")
        return root

    def attach(self, task, handle):
        task_id, attempt = task["task_id"], handle["attempt_id"]
        if handle.get("task_id") != task_id or handle.get("run_id") != self.journal.task(task_id)["run_id"]:
            raise ContractError("Container belongs to another task/run")
        self.root(task_id, handle)
        previous = next((r for r in self.rows() if r["task_id"] == task_id and r["generation"] == attempt), None)
        if previous is None or previous["phase"] in {"removed", "lost"}:
            self.journal.invalidate_workspace(task_id, "task_attempt_created_or_replaced")
            baseline = self.state_dir / "baselines" / task_id
            if baseline.is_symlink():
                raise ContractError("Invalid baseline directory")
            if baseline.exists():
                self.usage(baseline)
                shutil.rmtree(baseline)
        with self.journal.connect() as db:
            db.execute("""INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(task_id,generation) DO UPDATE SET manifest=excluded.manifest,
                phase='active',updated=excluded.updated,retained_at=NULL,reason=''""",
                (task_id, attempt, canonical(handle).decode(), "active", time.time(), None, ""))
        self.checked.pop((task_id, attempt), None)
        self.journal.event(task_id, "workspace:attached", {"attempt_id": attempt, "image_release_id": handle["image_release_id"]})

    def executor(self, handle):
        task = json.loads(self.journal.task(handle["task_id"])["manifest"])
        return self.executor_factory(self.config, self.state_dir, handle, task, self.relay, manager=self.manager)

    def export_pending(self, handle):
        """Recover volume-only artifacts before sealing, stopping or deleting."""
        task_id = handle["task_id"]
        root = self.root(task_id, handle)
        for record in self.journal.executions(task_id):
            ident = record["execution_id"]
            if not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise ContractError("Invalid execution identity during export")
            target = root / "artifacts" / ident
            # The host artifact directory is created before any container work.
            # It also binds interrupted queued/running records to this attempt.
            if record.get("evidence_exported") or not target.is_dir():
                continue
            if record.get("workspace_generation", handle["attempt_id"]) != handle["attempt_id"]:
                continue
            if record.get("artifact_dir") and Path(record["artifact_dir"]) != target:
                continue
            outcome = self.manager.export_execution(handle, ident, target) or {}
            if outcome.get("evidence_loss"):
                record.update(status="infra_error", reason="task_volume_missing", evidence_loss=outcome["evidence_loss"], reuse_invalidated=True)
                self.journal.event(task_id, "execution_evidence_lost", {"execution_id": ident, "attempt_id": handle["attempt_id"], "reason": outcome["evidence_loss"]})
            record.update(evidence_exported=True, artifact_dir=str(target))
            self.journal.execution(task_id, record["tool_id"], record["variant"], record)

    def check(self, executor):
        """Reap test identities before sealing. Codex must receive the finish reply."""
        handle = executor.generation
        key = (executor.task["task_id"], handle["attempt_id"])
        if key in self.checked:
            return self.checked[key]
        self.manager.cancel_event = None
        try:
            with resource_lock(self.state_dir, threading.Event()):
                processes = executor.stop_task()
                if processes.get("verified") is not True or processes.get("remaining"):
                    raise ContractError("Task process cleanup was not verified")
                self.export_pending(handle)
            result = {"status": "pass", "processes_stopped": True, "processes": processes,
                      "attempt_id": handle["attempt_id"], "completion": "exported_execution_evidence"}
        except Exception as exc:
            result = {"status": "infra_error", "processes_stopped": False, "reason": str(exc)}
        self.checked[key] = result
        atomic_json(self.root(key[0], handle) / "cleanup.json", result)
        return result

    def finish(self, executor):
        handle, task_id = executor.generation, executor.task["task_id"]
        attempt = handle["attempt_id"]
        row = next(r for r in self.rows() if r["task_id"] == task_id and r["generation"] == attempt)
        root = self.root(task_id, handle)
        try:
            self.manager.cancel_event = None
            with resource_lock(self.state_dir, threading.Event()):
                processes = executor.stop_task()
                if processes.get("verified") is not True or processes.get("remaining"):
                    raise ContractError("Cannot release a task with unconfirmed live processes")
                self.export_pending(handle)
                self.manager.purge_credentials(handle)
                stopped = self.manager.stop_task(handle)
                if stopped.get("verified") is not True:
                    raise ContractError("Task container stop was not confirmed")
            self.archive(row, root)
            current = self.journal.task(task_id)
            box = self.journal.outbox(task_id)
            if current["phase"] == "queued" and box is None:
                # A normal worker restart preserves this exact attempt. No
                # installed state is implicitly transferred to another run.
                self.phase(task_id, attempt, "active", "worker_restart")
                return
            self.manager.destroy_task(handle, keep_data=True)
            self.phase(task_id, attempt, "retained", "task_finished")
            if current["phase"] == "cancelled" or box and self.journal.result_status(box) in {"pass", "cancelled"}:
                self.remove(next(r for r in self.rows() if r["task_id"] == task_id and r["generation"] == attempt), "task_finished")
        except Exception as exc:
            self.phase(task_id, attempt, "unsafe", str(exc))
            self.journal.event(task_id, "task_cleanup_failed", {"attempt_id": attempt, "error": str(exc)})
            self.recovered = False
        self.collect()

    def recover(self):
        if self.recovered:
            return
        self.discovery_errors = []
        # acquire_task persists Docker ownership before returning. A crash or
        # failed initialization can precede attach(), so recover its registry
        # entry as well as workspaces already seen by the worker.
        known = {(row["task_id"], row["generation"]) for row in self.rows()}
        for handle in self.manager.generations().values():
            if handle.get("state") == "removed" or not handle.get("attempt_id"):
                continue
            key = (handle["task_id"], handle["attempt_id"])
            if key in known:
                continue
            task_row = self.journal.task(handle["task_id"])
            if task_row is None or task_row["run_id"] != handle["run_id"]:
                self.discovery_errors.append("Unattached task container requires run reconciliation: " + handle["attempt_id"])
                continue
            self.root(handle["task_id"], handle)
            with self.journal.connect() as db:
                db.execute("INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)", (*key, canonical(handle).decode(), "active", time.time(), None, "recovered_before_attach"))
            self.journal.event(handle["task_id"], "workspace:discovered", {"attempt_id": handle["attempt_id"]})
        for row in self.rows():
            if row["phase"] == "removed":
                continue
            handle = json.loads(row["manifest"])
            if not handle.get("attempt_id"):
                self.discovery_errors.append("Legacy persistent workspace needs offline migration: " + row["generation"])
                continue
            try:
                root = self.root(row["task_id"], handle)
                if row["phase"] in {"retained", "cleaning", "cleanup_failed"}:
                    continue
                recovered = self.manager.recover_task(handle)
                if recovered.get("status") == "rebuild_required":
                    with resource_lock(self.state_dir, threading.Event()):
                        self.export_pending(handle)
                    self.archive(row, root)
                    self.journal.invalidate_workspace(row["task_id"], "task_container_lost", generation=row["generation"])
                    self.phase(row["task_id"], row["generation"], "lost", "container_lost")
                    continue
                if recovered.get("status") != "same_attempt":
                    raise ContractError("Unknown task recovery outcome")
                executor = self.executor(handle)
                with resource_lock(self.state_dir, threading.Event()):
                    report = executor.stop_task()
                    if report.get("verified") is not True or report.get("remaining"):
                        raise ContractError("Interrupted task cleanup failed")
                    executor.stop_codex()
                    self.export_pending(handle)
                self.archive(row, root)
                task_row = self.journal.task(row["task_id"])
                if task_row["phase"] in {"complete", "cancelled", "incomplete", "publishing"}:
                    self.finish(executor)
                else:
                    self.phase(row["task_id"], row["generation"], "active", "recovered")
            except Exception as exc:
                self.phase(row["task_id"], row["generation"], "unsafe", str(exc))
                self.discovery_errors.append(str(exc))
        self.recovered = not self.discovery_errors

    @staticmethod
    def usage(root):
        """Conservative logical bytes: reflink sharing must not defeat the cap."""
        total = 0
        if not root.exists():
            return total
        for parent, directories, files in os.walk(root, followlinks=False):
            for name in [*directories, *files]:
                path = Path(parent) / name
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode) and os.path.ismount(path):
                    raise ContractError("Refusing mounted task workspace contents")
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def archive(self, row, root):
        """Keep execution evidence outside disposable trees, including failures."""
        box = self.journal.outbox(row["task_id"])
        sealed = Path(box["payload_path"]).parent if box and self.journal.result_status(box) is not None else None
        for record in self.journal.executions(row["task_id"]):
            ident = record["execution_id"]
            if not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise ContractError("Invalid recorded execution identity")
            if not record.get("artifact_dir"):
                # A killed worker may only have persisted the queued/running
                # record; its already-created log directory still needs saving.
                possible = root / "artifacts" / ident
                if not possible.is_dir():
                    continue
                record.update(artifact_dir=str(possible), status="infra_error", reason="worker_interrupted")
            source = Path(record["artifact_dir"])
            if not source.is_relative_to(root):
                continue
            if source != root / "artifacts" / ident:
                raise ContractError("Execution evidence has an unexpected task path")
            if not source.is_dir() or source.is_symlink():
                raise ContractError("Cannot collect workspace with missing execution evidence")
            target = self.state_dir / "workspace-evidence" / row["task_id"] / ident
            files_to_preserve = []
            for parent, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
                for name in files:
                    path = Path(parent) / name
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                    files_to_preserve.append(path)
            existing = sealed / "evidence" / ident if sealed else None
            if existing is not None and all(
                    (existing / path.relative_to(source)).is_file()
                    and file_digest(existing / path.relative_to(source)) == file_digest(path)
                    for path in files_to_preserve):
                target = existing
            else:
                required = sum(path.stat().st_size for path in files_to_preserve)
                if shutil.disk_usage(self.state_dir).free - required < self.config.get("minimum_free_bytes", 10 * 1024**3):
                    raise ContractError("Insufficient free state space to preserve execution evidence")
                for path in files_to_preserve:
                    dest = target / path.relative_to(source)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() and file_digest(dest) != file_digest(path):
                        raise ContractError("Archived execution evidence changed")
                    temporary = dest.with_name("." + dest.name + ".copying")
                    shutil.copyfile(path, temporary)
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(temporary, dest)
            # SQLite may become durable before buffered evidence copies. Flush
            # files and their directories before redirecting records/deleting
            # scratch, including when reusing an already sealed evidence copy.
            directories = set()
            for path in files_to_preserve:
                dest = target / path.relative_to(source)
                with dest.open("rb") as stream:
                    os.fsync(stream.fileno())
                for parent in dest.parents:
                    if not parent.is_relative_to(self.state_dir):
                        break
                    directories.add(parent)
            for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            record["artifact_dir"] = str(target)
            self.journal.execution(row["task_id"], record["tool_id"], record["variant"], record)

    def remove(self, row, reason):
        handle = json.loads(row["manifest"])
        root = self.root(row["task_id"], handle)
        with resource_lock(self.state_dir, threading.Event()):
            current = next(r for r in self.rows() if r["task_id"] == row["task_id"] and r["generation"] == row["generation"])
            if current["phase"] not in {"retained", "cleaning", "cleanup_failed", "lost"}:
                raise ContractError("Task became active during collection")
            self.phase(row["task_id"], row["generation"], "cleaning", reason)
            self.usage(root)
            self.export_pending(handle)
            self.archive(row, root)
            self.manager.destroy_task(handle, keep_data=False)
            self.journal.invalidate_workspace(row["task_id"], reason, generation=row["generation"])
            if root.exists():
                shutil.rmtree(root)
            self.phase(row["task_id"], row["generation"], "removed", reason)

    def collect(self, *, now=None):
        now = time.time() if now is None else now
        errors, candidates, total = list(self.discovery_errors), [], 0
        for row in self.rows():
            if row["phase"] == "removed":
                continue
            handle = json.loads(row["manifest"])
            try:
                root = self.root(row["task_id"], handle)
                host_bytes = self.usage(root)
                volume_bytes = int(self.manager.task_usage(handle))
                size = host_bytes + volume_bytes
                total += size
                if row["phase"] in {"unsafe"}:
                    errors.append(row["task_id"] + ": " + row["reason"])
                elif row["phase"] in {"retained", "cleaning", "cleanup_failed", "lost"}:
                    candidates.append((row, size))
            except Exception as exc:
                errors.append(str(exc))
        for row, size in candidates:
            elapsed = now - (row["retained_at"] or row["updated"])
            reason = "retention_expired" if elapsed >= self.retention * 3600 else "disk_budget" if total > self.budget else "retry_cleanup" if row["phase"] in {"cleaning", "cleanup_failed", "lost"} else None
            if reason is None:
                continue
            try:
                self.remove(row, reason)
                total -= size
            except Exception as exc:
                self.phase(row["task_id"], row["generation"], "cleanup_failed", str(exc))
                errors.append(str(exc))
        free = shutil.disk_usage(self.state_dir).free
        minimum = self.config.get("minimum_free_bytes", 10 * 1024**3)
        if total > self.budget:
            errors.append("Protected active task data exceeds the scratch budget")
        if free < minimum:
            errors.append("Insufficient free state space for new tasks")
        durable = sum(self.usage(self.state_dir / name) for name in ("tasks", "workspace-evidence", "executor-records"))
        result = {"schema": "triton-anchor-workspace-health/v2", "status": "error" if errors else "healthy",
                  "runtime": "task-containers", "at": now, "scratch_bytes": total, "budget_bytes": self.budget,
                  "durable_evidence_bytes": durable, "state_free_bytes": free, "minimum_free_bytes": minimum,
                  "errors": errors, "workspaces": [{k: r[k] for k in ("task_id", "generation", "phase", "reason")} for r in self.rows()]}
        atomic_json(self.state_dir / "workspace-health.json", result)
        return result
