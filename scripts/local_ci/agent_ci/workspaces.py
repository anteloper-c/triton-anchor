"""Task scratch cleanup; a Worker restart always starts a fresh unsealed run."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .protocol import ContractError, atomic_json


class TaskWorkspaces:
    def __init__(self, config, journal, manager, relay, executor_factory):
        self.config, self.journal, self.manager = config, journal, manager
        self.relay, self.executor_factory = relay, executor_factory
        self.state_dir = Path(config["state_dir"])
        self.recovered = False
        self.errors = []

    def root(self, task_id, handle):
        return self.journal.run_dir(task_id, handle["run_id"])

    def attach(self, task, handle):
        with self.journal.guard:
            state = self.journal._state(task["task_id"])
            state["container"] = handle
            self.journal._write(task["task_id"], state)

    def executor(self, handle):
        task = json.loads(
            (self.root(handle["task_id"], handle) / "task.json").read_text()
        )
        return self.executor_factory(
            self.config, self.state_dir, handle, task, self.relay, manager=self.manager
        )

    def export_pending(self, handle):
        task_id = handle["task_id"]
        if self.journal.task(task_id)["run_id"] != handle["run_id"]:
            return
        for record in self.journal.executions(task_id):
            if record.get("evidence_exported"):
                continue
            target = self.root(task_id, handle) / "artifacts" / record["execution_id"]
            if not target.exists():
                continue
            outcome = (
                self.manager.export_execution(handle, record["execution_id"], target)
                or {}
            )
            record.update(artifact_dir=str(target), evidence_exported=True)
            if outcome.get("evidence_loss") or record["status"] in {
                "running",
                "queued",
            }:
                record.update(status="infra_error", reason="execution_interrupted")
            self.journal.execution(
                task_id, record["tool_id"], record["variant"], record
            )

    def check(self, executor):
        try:
            processes = executor.stop_task()
            if not processes.get("verified"):
                raise ContractError("Execution process groups could not be stopped")
            self.export_pending(executor.generation)
            return {"status": "pass", "processes_stopped": True}
        except Exception as exc:
            return {"status": "infra_error", "reason": str(exc)}

    def finish(self, executor, *, recovering=False):
        handle = executor.generation
        try:
            if recovering:
                # The previous worker's process table is gone. Stop the entire
                # owned container before cleanup; docker exec cannot run in an
                # already stopped/missing container and must not gate recovery.
                report = self.manager.stop_task(handle)
            else:
                executor.stop_task()
                executor.stop_codex()
                self.export_pending(handle)
                native = self.root(handle["task_id"], handle) / "artifacts/native-final"
                try:
                    outcome = executor.export_native_evidence(native)
                except Exception as exc:
                    outcome = {"evidence_loss": str(exc)}
                atomic_json(native.parent / "native-export.json", outcome)
                self.manager.purge_credentials(handle)
                report = self.manager.stop_task(handle)
            if not report.get("verified"):
                raise ContractError("Task container stop not confirmed")
            self.manager.destroy_task(handle, keep_data=False)
            with self.journal.guard:
                state = self.journal._state(handle["task_id"])
                if state["run_id"] == handle["run_id"]:
                    state["cleanup"] = {"status": "complete", "recovered": recovering}
                    if recovering and not state.get("delivery"):
                        state["cleanup"]["evidence_loss"] = (
                            "Unexported scratch evidence was unavailable after Worker restart; existing host logs and artifacts retained"
                        )
                    self.journal._write(handle["task_id"], state)
            return True
        except Exception as exc:
            self.errors.append(str(exc))
            self.journal.event(
                handle["task_id"], "cleanup_pending", {"reason": str(exc)}
            )
            self.recovered = False
            return False

    def recover(self):
        if self.recovered:
            return
        self.errors = []
        handles = [
            h
            for h in self.manager.generations().values()
            if h.get("attempt_id") and h.get("state") != "removed"
        ]
        for handle in handles:
            if not self.finish(self.executor(handle), recovering=True):
                return
        for row in self.journal.tasks():
            directory = self.journal.run_dir(row["task_id"])
            sealed = directory / "sealed/result.json"
            if sealed.exists() and self.journal.delivery(row["task_id"]) is None:
                import hashlib

                self.journal.queue_result(
                    row["task_id"],
                    sealed,
                    hashlib.sha256(sealed.read_bytes()).hexdigest(),
                )
            elif self.journal.delivery(row["task_id"]) is None:
                self.journal.restart(row["task_id"])
        self.recovered = True

    def collect(self, *, now=None):
        from ops_maint.retention import retain_local

        try:
            retention = retain_local(self.config, now=now)
        except ImportError:
            retention = {}
        free = shutil.disk_usage(self.state_dir).free
        errors = list(self.errors)
        if free < self.config.get("minimum_free_bytes", 10 * 1024**3):
            errors.append("Insufficient free space for a new task")
        if retention.get("pause_intake"):
            errors.append(
                "Evidence budget reached; required pending evidence is protected"
            )
        result = {
            "status": "error" if errors else "healthy",
            "errors": errors,
            "state_free_bytes": free,
        }
        atomic_json(self.state_dir / "workspace-health.json", result)
        return result
