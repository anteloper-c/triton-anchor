"""Trusted task workspace lifecycle; sealed delivery evidence is never collected."""
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
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.digest()


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
        self.journal.event(task_id, "workspace:" + phase, {"generation": generation, "reason": reason})

    def root(self, task_id, generation):
        if not re.fullmatch(r"[a-f0-9]{64}", task_id):
            raise ContractError("Invalid workspace task identity")
        workspace = Path(generation["workspace_host"])
        if not workspace.is_absolute() or workspace.is_symlink():
            raise ContractError("Invalid generation workspace")
        root = workspace / "tasks" / task_id
        for path in (workspace, workspace / "tasks", root):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ContractError("Workspace cleanup path contains a symlink or non-directory")
        if root.exists() and os.path.ismount(root):
            raise ContractError("Refusing mounted task workspace")
        if root.resolve().is_relative_to(self.state_dir.resolve()) or self.state_dir.resolve().is_relative_to(root.resolve()):
            raise ContractError("Task scratch and durable state must not overlap")
        return root

    def attach(self, task, generation):
        task_id, ident = task["task_id"], generation["generation"]
        root = self.root(task_id, generation)
        previous = next((r for r in self.rows() if r["task_id"] == task_id and r["generation"] == ident), None)
        if not previous or previous["phase"] == "removed" or not root.exists():
            self.journal.invalidate_workspace(task_id, "workspace_created_or_replaced")
            baseline = self.state_dir / "baselines" / task_id
            # Sealed reports keep historical baseline evidence. Never import a
            # baseline into a newly created installation merely by recipe hash.
            if baseline.is_symlink():
                raise ContractError("Invalid baseline directory")
            if baseline.exists():
                shutil.rmtree(baseline)
        with self.journal.connect() as db:
            db.execute("""INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(task_id,generation) DO UPDATE SET manifest=excluded.manifest,
                phase='active',updated=excluded.updated,retained_at=NULL,reason=''""",
                (task_id, ident, canonical(generation).decode(), "active", time.time(), None, ""))
        self.checked.pop((task_id, ident), None)
        self.manager.mark_dirty(ident, task_id)
        self.journal.event(task_id, "workspace:attached", {"generation": ident})

    def check(self, executor):
        """Stop candidate processes and validate reuse before sealing a result."""
        task_id, ident = executor.task["task_id"], executor.generation["generation"]
        key = (task_id, ident)
        if key in self.checked:
            return self.checked[key]
        # PR cancellation must stop candidate work, not cancel trusted cleanup.
        self.manager.cancel_event = None
        stopped = False
        try:
            with resource_lock(self.state_dir, threading.Event()):
                processes = executor.stop_task()
                if processes.get("verified") is not True or processes.get("remaining"):
                    raise ContractError("Task process cleanup was not verified")
            # Device validation may itself launch descendants; only its final
            # reaper or a confirmed container stop proves this phase finished.
            stopped = False
            validation = self.manager.validate_reuse(ident)
            with resource_lock(self.state_dir, threading.Event()):
                after_probe = executor.stop_task()
                stopped = after_probe.get("verified") is True and not after_probe.get("remaining")
                if not stopped or after_probe.get("cleaned_pid_count", 0):
                    raise ContractError("Post-task validation left processes behind")
            result = {"status": "pass", "processes_stopped": True,
                      "processes": processes, "validation": {"generation": ident,
                      "validated_at": validation.get("reuse_validated_at"), "reusable": True}}
        except Exception as exc:
            reason = str(exc)
            try:
                quarantine = self.manager.quarantine(ident, reason)
                stopped = stopped or quarantine.get("stopped") is True
            except Exception as quarantine_error:
                reason += "; quarantine: " + str(quarantine_error)
            result = {"status": "infra_error", "processes_stopped": stopped, "reason": reason}
        self.checked[key] = result
        self.journal.event(task_id, "workspace:reuse_check", {"generation": ident, **result})
        return result

    def finish(self, executor):
        task_id, ident = executor.task["task_id"], executor.generation["generation"]
        result = self.check(executor)
        if not result["processes_stopped"]:
            self.phase(task_id, ident, "unsafe", result.get("reason", "Processes remain"))
            return
        self.phase(task_id, ident, "retained", result.get("reason", "execution_finished"))
        # Keep the generation lease until evidence is durable. A concurrent
        # daily generation collector must never delete an unarchived log tree.
        row = next(r for r in self.rows() if r["task_id"] == task_id and r["generation"] == ident)
        try:
            with resource_lock(self.state_dir, threading.Event()):
                root = self.root(task_id, executor.generation)
                self.usage(root)
                if root.exists():
                    self.archive(row, root)
            self.collect()
            self.release_if_matches(task_id, ident)
        except Exception as exc:
            self.phase(task_id, ident, "cleanup_failed", str(exc))
            self.manager.quarantine(ident, "task_evidence_preservation_failed")
            self.collect()

    def release_if_matches(self, task_id, generation):
        if self.manager.leases().get(task_id, {}).get("generation") == generation:
            self.manager.release(task_id)

    def recover(self):
        """Called under the singleton worker lock, including when Gitee is down."""
        if self.recovered and not self.discovery_errors and not any(row["phase"] == "unsafe" for row in self.rows()):
            return
        self.discovery_errors = []
        leases = self.manager.leases()
        known = {(r["task_id"], r["generation"]) for r in self.rows()}
        tasks = {r["task_id"] for r in self.journal.tasks(active=False)}
        # Existing successful tasks have no lease. Register their scratch too,
        # otherwise an upgrade would leave old venvs outside the retention cap.
        for ident, generation in self.manager.generations().items():
            directory = Path(generation["workspace_host"]) / "tasks"
            if not directory.exists():
                continue
            if directory.is_symlink():
                self.discovery_errors.append({"generation": ident, "error": "Task parent is a symlink"})
                continue
            for root in directory.iterdir():
                task_id = root.name
                if (task_id, ident) in known:
                    continue
                if task_id not in tasks or not re.fullmatch(r"[a-f0-9]{64}", task_id):
                    self.discovery_errors.append({"generation": ident, "error": "Unregistered task directory; manual inspection required"})
                    continue
                with self.journal.connect() as db:
                    db.execute("INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)",
                               (task_id, ident, canonical(generation).decode(), "active", time.time(), None, "legacy_workspace"))
                known.add((task_id, ident))
        # Import pre-upgrade leases before releasing them; the old generation is
        # the only correct place to reap children left by a killed worker.
        for task_id, lease in leases.items():
            ident = lease["generation"]
            if (task_id, ident) not in known:
                generation = self.manager.generation(ident)
                with self.journal.connect() as db:
                    db.execute("INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)",
                               (task_id, ident, canonical(generation).decode(), "active", time.time(), None, "legacy_lease"))
        for row in self.rows():
            leased = leases.get(row["task_id"], {}).get("generation") == row["generation"]
            if row["phase"] not in {"active", "unsafe", "cleaning", "cleanup_failed"} and not leased:
                continue
            task = json.loads(self.journal.task(row["task_id"])["manifest"])
            generation = json.loads(row["manifest"])
            self.checked.pop((row["task_id"], row["generation"]), None)
            executor = self.executor_factory(self.config, self.state_dir, generation, task, self.relay)
            result = self.check(executor)
            if result["processes_stopped"]:
                phase = row["phase"] if row["phase"] in {"cleaning", "cleanup_failed"} else "retained"
                self.phase(row["task_id"], row["generation"], phase, "worker_recovery")
                try:
                    with resource_lock(self.state_dir, threading.Event()):
                        root = self.root(row["task_id"], generation)
                        self.usage(root)
                        if root.exists():
                            self.archive(row, root)
                    self.release_if_matches(row["task_id"], row["generation"])
                except Exception as exc:
                    self.phase(row["task_id"], row["generation"], "cleanup_failed", str(exc))
                    self.manager.quarantine(row["generation"], "recovery_evidence_preservation_failed")
            else:
                self.phase(row["task_id"], row["generation"], "unsafe", result.get("reason", "recovery_failed"))
        self.recovered = True

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
        generation = json.loads(row["manifest"])
        root = self.root(row["task_id"], generation)
        # Singleton poll.lock excludes CLI resume; resource.lock excludes tools
        # and environment rotation. Only confirmed stopped tasks are eligible.
        with resource_lock(self.state_dir, threading.Event()):
            current = next(r for r in self.rows() if r["task_id"] == row["task_id"] and r["generation"] == row["generation"])
            if current["phase"] not in {"retained", "cleaning", "cleanup_failed"}:
                raise ContractError("Workspace became active during collection")
            self.phase(row["task_id"], row["generation"], "cleaning", reason)
            self.usage(root)  # Reject nested mounts before any recursive delete.
            if root.exists():
                self.archive(row, root)
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
            try:
                root = self.root(row["task_id"], json.loads(row["manifest"]))
                size = self.usage(root)
                total += size
                if row["phase"] in {"active", "unsafe"}:
                    if row["phase"] == "unsafe":
                        errors.append({"task_id": row["task_id"], "error": row["reason"]})
                    continue
                task = self.journal.task(row["task_id"])
                box = self.journal.outbox(row["task_id"])
                successful = box and self.journal.result_status(box) == "pass"
                immediate = successful or task["phase"] == "cancelled" or row["phase"] in {"cleaning", "cleanup_failed"}
                expired = now - (row["retained_at"] or row["updated"]) >= self.retention * 3600
                candidates.append((not (immediate or expired), row["retained_at"] or row["updated"], row, size,
                                   "sealed_success" if successful else "cancelled" if task["phase"] == "cancelled" else "retry_cleanup" if immediate else "retention_expired" if expired else "disk_budget"))
            except Exception as exc:
                try:
                    self.manager.quarantine(row["generation"], "Workspace inspection failed: " + str(exc))
                except Exception:
                    pass
                errors.append({"task_id": row["task_id"], "error": str(exc)})
        for optional, _, row, size, reason in sorted(candidates, key=lambda item: (item[0], item[1])):
            if optional and total <= self.budget:
                continue
            try:
                self.remove(row, reason)
                self.release_if_matches(row["task_id"], row["generation"])
                total -= size
            except Exception as exc:
                self.phase(row["task_id"], row["generation"], "cleanup_failed", str(exc))
                try:
                    self.manager.quarantine(row["generation"], "Workspace cleanup failed: " + str(exc))
                except Exception:
                    pass
                errors.append({"task_id": row["task_id"], "error": str(exc)})
        free = shutil.disk_usage(self.state_dir).free
        minimum = self.config.get("minimum_free_bytes", 10 * 1024**3)
        durable = sum(self.usage(self.state_dir / path) for path in ("workspace-evidence", "tasks"))
        if free < minimum:
            errors.append({"error": "Insufficient free durable-state disk space"})
        report = {"collected_at": now, "status": "error" if errors or total > self.budget else "healthy",
                  "logical_bytes": total, "max_bytes": self.budget, "retention_hours": self.retention,
                  "state_free_bytes": free, "minimum_free_bytes": minimum, "durable_evidence_bytes": durable,
                  "errors": errors, "workspaces": [{k: r[k] for k in ("task_id", "generation", "phase", "reason")} for r in self.rows()]}
        atomic_json(self.state_dir / "workspace-health.json", report)
        return report
