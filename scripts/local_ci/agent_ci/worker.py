#!/usr/bin/env python3
"""Run Codex through evidence sealing, then deliver its immutable outbox to Gitee."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))

from agent_ci.codex import CodexDriver
from agent_ci.control import validate_control_revision
from agent_ci.executor import DockerExecutor
from agent_ci.policy import changed_files, minimum_checks
from agent_ci.protocol import ContractError, RESULT_SCHEMA, atomic_json, validate_task
from agent_ci.relay import GitRelay
from agent_ci.state import Journal
from agent_ci.supervisor import Supervisor, ToolService


class Worker:
    def __init__(self, config: dict, *, relay=None, manager=None, driver=None, executor_factory=None):
        self.config, self.state_dir = config, Path(config["state_dir"])
        self.journal = Journal(self.state_dir)
        self.relay = relay or GitRelay(config["gitee_repo_url"], self.state_dir / "relay", allow_local=config.get("simulation", False))
        if manager is None:
            from environments.manager import EnvironmentManager
            manager = EnvironmentManager(config, self.state_dir)
        self.manager = manager
        self.driver = driver or CodexDriver(config, self.state_dir)
        self.executor_factory = executor_factory or DockerExecutor
        self.stop_event = threading.Event()
        self.active = None

    def heartbeat(self, **extra):
        atomic_json(self.state_dir / "health/worker.json", {
            "schema": "triton-anchor-worker-health/v4", "worker_id": self.config.get("worker_id", "local-ci"),
            "heartbeat_at": time.time(), "pid": os.getpid(), "tasks": [
                {"task_id": r["task_id"], "phase": r["phase"], "updated": r["updated"]} for r in self.journal.tasks()], **extra})

    def watch(self, supervisor, done):
        while not done.wait(self.config.get("poll_interval_seconds", 60)):
            try:
                self.relay.refresh()
                valid, reason = self.relay.validity(supervisor.task)
                if not valid:
                    supervisor.cancel(reason)
                self.heartbeat(active_task=supervisor.task["task_id"])
            except Exception as exc:
                self.journal.event(supervisor.task["task_id"], "control_channel_unavailable", {"error": str(exc)})
                self.heartbeat(control_channel="unreachable")

    def process(self, task):
        validate_task(task, tuple(self.config.get("repositories", ["likehupochuan/triton-anchor"])))
        valid, _ = self.relay.validity(task)
        if not valid:
            return
        row = self.journal.register(task)
        if row["phase"] in {"complete", "cancelled", "incomplete", "publishing"}:
            return
        run_dir = self.state_dir / "tasks" / task["task_id"] / row["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        self.journal.phase(task["task_id"], "preparing")
        supervisor, watcher = None, None
        done = threading.Event()
        prepare_cancel = threading.Event()
        worker = self

        class Preparing:
            def __init__(self):
                self.task = task

            def cancel(self, reason):
                prepare_cancel.set()
                worker.journal.event(task["task_id"], "preparation_cancelled", {"reason": reason})
                if supervisor:
                    supervisor.cancel(reason)

        preparation = Preparing()
        self.active = preparation
        self.manager.cancel_event = prepare_cancel
        watcher = threading.Thread(target=self.watch, args=(preparation, done), daemon=True)
        watcher.start()
        try:
            validate_control_revision(self.config, task)
            generation = self.manager.acquire(task["task_id"], task["target_branch"], task["llvm_hash"])
            if prepare_cancel.is_set():
                raise InterruptedError("Task cancelled during environment preparation")
            executor = self.executor_factory(self.config, self.state_dir, generation, task, self.relay)
            checkout = executor.prepare()
            changes = changed_files(checkout, task["base_sha"], task["tested_sha"])
            if not changes and task["event_kind"] != "pull_request":
                changes = [{"path": "branch-validation", "old_path": "branch-validation", "mode": "100644"}]
            policy = minimum_checks(changes, backend_enabled=generation["backend_enabled"], full=task["full"])
            atomic_json(run_dir / "policy.json", policy)
            atomic_json(run_dir / "task.json", task)
            supervisor = Supervisor(task, policy, self.journal, executor, run_dir, changes=changes)
            supervisor.recover()
            self.active = supervisor
            self.journal.phase(task["task_id"], "running")
            socket_path = Path(self.config.get("rpc_socket_dir", "/tmp/local-ci-rpc")) / (task["task_id"][:16] + "-" + row["run_id"][-8:] + ".sock")
            with ToolService(supervisor, socket_path) as service:
                for attempt in range(self.config.get("codex_attempts", 3)):
                    if self.stop_event.is_set() or supervisor.cancelled.is_set() or supervisor.closed:
                        break
                    try:
                        outcome = self.driver.run(supervisor, service, recovery="Continue from actual records; do not repeat successful checks." if attempt else "")
                        self.journal.event(task["task_id"], "codex_attempt", {"attempt": attempt + 1, **outcome})
                    except Exception as exc:
                        self.journal.event(task["task_id"], "codex_error", {"attempt": attempt + 1, "error": str(exc)})
                    if not supervisor.closed and attempt + 1 < self.config.get("codex_attempts", 3):
                        if self.stop_event.wait(self.config.get("retry_delay_seconds", 30) * (attempt + 1)):
                            break
                supervisor.close()
                if self.stop_event.is_set() and not supervisor.closed:
                    self.journal.phase(task["task_id"], "queued", {"reason": "worker_restart"})
                elif not supervisor.closed:
                    supervisor.finish("Necessary Codex work remained incomplete after bounded recovery.")
        except Exception as exc:
            self.journal.event(task["task_id"], "preparation_failed", {"error": str(exc)})
            if self.stop_event.is_set():
                self.journal.phase(task["task_id"], "queued", {"reason": "worker_restart"})
                return
            published = run_dir / "published"
            result = {"schema": RESULT_SCHEMA, "task": task, "run_id": row["run_id"], "status": "infra_error",
                      "summary": "Environment or worker preparation failed", "required_checks": [], "checks": [],
                      "reviews": {}, "findings": [], "blockers": [], "performance": [],
                      "unfinished": ["environment preparation", str(exc)], "environment": {},
                      "publication": {"status": "pending_upload", "completion_requires": ["gitee_upload"]}}
            atomic_json(published / "result.json", result)
            self.journal.queue_result(task["task_id"], published / "result.json", hashlib.sha256((published / "result.json").read_bytes()).hexdigest())
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=5)
            if supervisor:
                supervisor.close()
            self.active = None
            self.manager.cancel_event = None
            self.manager.release(task["task_id"])
            self.heartbeat()

    def deliver(self, row):
        task = json.loads(row["manifest"])
        if self.journal.task(task["task_id"])["run_id"] != row["run_id"]:
            return  # A stale caller must not upload the explicitly resumed run.
        box = self.journal.outbox(task["task_id"])
        if not box:
            return
        if box["published"] is not None:
            self.journal.published(task["task_id"])
            return
        result_path = Path(box["payload_path"])
        payload = result_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != box["digest"]:
            raise ContractError("Sealed result digest changed")
        result = json.loads(payload)
        if result.get("task") != task or result.get("run_id") != row["run_id"] or result.get("schema") != RESULT_SCHEMA:
            raise ContractError("Sealed result identity differs from the pending task/run")
        self.relay.refresh()
        valid, reason = self.relay.validity(task)
        if not valid:
            self.journal.phase(task["task_id"], "cancelled", {"reason": reason})
            return
        uploaded_digest = self.relay.publish_result(task, row["run_id"], result_path.parent)
        if uploaded_digest != box["digest"]:
            raise ContractError("Uploaded result digest differs from sealed outbox")
        self.journal.published(task["task_id"])

    def retry_delivery(self, row):
        """One durable upload attempt. Never invokes Codex or task tools."""
        try:
            self.deliver(row)
        except Exception as exc:
            count = self.journal.publication_failure(row["task_id"])
            detail = {"upload_error": str(exc), "attempts": count, "reason": "retry_saved_upload"}
            self.journal.event(row["task_id"], "delivery_error", detail)
            self.journal.phase(row["task_id"], "publishing", detail)

    def scan(self):
        self.relay.refresh()
        attempted = set()
        for row in self.journal.tasks():
            if row["phase"] == "publishing":
                self.retry_delivery(row)
                attempted.add(row["task_id"])
        for task in self.relay.tasks():
            if self.stop_event.is_set():
                break
            try:
                self.process(task)
                row = self.journal.task(task["task_id"])
                if row["phase"] == "publishing" and row["task_id"] not in attempted:
                    self.retry_delivery(row)
                    attempted.add(row["task_id"])
            except Exception as exc:
                self.journal.event(task.get("task_id", "invalid"), "task_error", {"error": str(exc)})
        self.heartbeat()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("LOCAL_CI_CONFIG_JSON", "/opt/local-ci/config.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resume", metavar="TASK_ID")
    args = parser.parse_args(argv)
    worker = Worker(json.loads(Path(args.config).read_text()))
    if args.resume:
        worker.journal.resume(args.resume)
        return 0
    import fcntl
    with (worker.state_dir / "poll.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another worker owns the poll lock", file=sys.stderr)
            return 2
        def stop(signum, frame):
            worker.stop_event.set()
            if worker.active:
                worker.active.cancel("worker_shutdown")
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not worker.stop_event.is_set():
            try:
                worker.scan()
            except Exception as exc:
                worker.heartbeat(control_channel="unreachable", error=str(exc))
            if args.once:
                break
            worker.stop_event.wait(worker.config.get("poll_interval_seconds", 60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
