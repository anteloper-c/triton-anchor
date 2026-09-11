#!/usr/bin/env python3
"""Run Codex through evidence sealing, then deliver its immutable sealed delivery to Gitee."""

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
from agent_ci.executor import DockerExecutor, resource_lock
from agent_ci.policy import changed_files, minimum_checks
from agent_ci.protocol import ContractError, RESULT_SCHEMA, atomic_json, validate_task
from agent_ci.relay import GitRelay
from agent_ci.state import Journal
from agent_ci.supervisor import Supervisor, ToolService
from agent_ci.workspaces import TaskWorkspaces


class Worker:
    def __init__(
        self,
        config: dict,
        *,
        relay=None,
        manager=None,
        driver=None,
        executor_factory=None,
    ):
        self.config, self.state_dir = config, Path(config["state_dir"])
        self.journal = Journal(self.state_dir)
        self.relay = relay or GitRelay(
            config["gitee_repo_url"],
            self.state_dir / "relay",
            allow_local=config.get("simulation", False),
        )
        if manager is None:
            from ops_maint.manager import EnvironmentManager

            manager = EnvironmentManager(config, self.state_dir)
        self.manager = manager
        self.driver = driver or CodexDriver(config, self.state_dir)
        self.executor_factory = executor_factory or DockerExecutor
        self.stop_event = threading.Event()
        self.active = None
        self.workspaces = TaskWorkspaces(
            config, self.journal, manager, self.relay, self.executor_factory
        )

    def make_executor(self, generation, task):
        return self.executor_factory(
            self.config,
            self.state_dir,
            generation,
            task,
            self.relay,
            manager=self.manager,
        )

    def heartbeat(self, **extra):
        atomic_json(
            self.state_dir / "health/worker.json",
            {
                "schema": "triton-anchor-worker-health/v4",
                "worker_id": self.config.get("worker_id", "local-ci"),
                "heartbeat_at": time.time(),
                "pid": os.getpid(),
                "tasks": [
                    {
                        "task_id": r["task_id"],
                        "phase": r["phase"],
                        "updated": r["updated"],
                    }
                    for r in self.journal.tasks()
                ],
                **extra,
            },
        )

    def watch(self, supervisor, done):
        while not done.wait(self.config.get("poll_interval_seconds", 60)):
            try:
                self.relay.refresh()
                valid, reason = self.relay.validity(supervisor.task)
                if not valid:
                    supervisor.cancel(reason)
                if getattr(supervisor, "control_available", None) is not None:
                    supervisor.control_available.set()
                self.heartbeat(active_task=supervisor.task["task_id"])
            except Exception as exc:
                if getattr(supervisor, "control_available", None) is not None:
                    supervisor.control_available.clear()
                self.journal.event(
                    supervisor.task["task_id"],
                    "control_channel_unavailable",
                    {"error": str(exc)},
                )
                self.heartbeat(control_channel="unreachable")

    def process(self, task):
        self.workspaces.recover()
        maintenance = self.workspaces.collect()
        if maintenance["status"] != "healthy":
            raise ContractError(
                "Workspace maintenance blocks new execution; inspect workspace-health.json"
            )
        validate_task(
            task,
            tuple(self.config.get("repositories", ["likehupochuan/triton-anchor"])),
        )
        valid, _ = self.relay.validity(task)
        if not valid:
            return
        row = self.journal.register(task)
        if row["phase"] in {"published", "publish_pending"}:
            return
        run_dir = self.state_dir / "runs" / task["task_id"] / row["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        self.journal.phase(task["task_id"], "preparing")
        supervisor, watcher, executor = None, None, None
        generation = None
        done = threading.Event()
        prepare_cancel = threading.Event()
        control_available = threading.Event()
        control_available.set()
        worker = self

        class Preparing:
            def __init__(self):
                self.task = task

            @property
            def control_available(self):
                return control_available

            def cancel(self, reason):
                prepare_cancel.set()
                worker.journal.event(
                    task["task_id"], "preparation_cancelled", {"reason": reason}
                )
                if supervisor:
                    supervisor.cancel(reason)

        preparation = Preparing()
        self.active = preparation
        self.manager.cancel_event = prepare_cancel
        watcher = threading.Thread(
            target=self.watch, args=(preparation, done), daemon=True
        )
        watcher.start()
        try:
            validate_control_revision(self.config, task)
            rpc_directory = (
                self.state_dir / "work" / task["task_id"] / row["run_id"] / "rpc"
            )
            rpc_directory.mkdir(parents=True, exist_ok=True)
            socket_path = rpc_directory / "broker.sock"
            generation = self.manager.acquire_task(
                task, row["run_id"], rpc_directory=rpc_directory
            )
            self.workspaces.attach(task, generation)
            if prepare_cancel.is_set():
                raise InterruptedError("Task cancelled during environment preparation")
            executor = self.make_executor(generation, task)
            # Verify the cleanup capability before any candidate code executes.
            with resource_lock(self.state_dir, prepare_cancel):
                executor.stop_task()
            checkout = executor.prepare()
            changes = changed_files(checkout, task["base_sha"], task["tested_sha"])
            if not changes and task["event_kind"] != "pull_request":
                changes = [
                    {
                        "path": "branch-validation",
                        "old_path": "branch-validation",
                        "mode": "100644",
                    }
                ]
            policy = minimum_checks(
                changes,
                backend_enabled=generation["backend_enabled"],
                full=task["full"],
            )
            atomic_json(run_dir / "policy.json", policy)
            atomic_json(run_dir / "task.json", task)
            supervisor = Supervisor(
                task,
                policy,
                self.journal,
                executor,
                run_dir,
                changes=changes,
                before_seal=lambda: self.workspaces.check(executor),
            )
            supervisor.control_available = control_available
            supervisor.recover()
            self.active = supervisor
            self.journal.phase(task["task_id"], "running")
            with ToolService(supervisor, socket_path) as service:
                for attempt in range(self.config.get("codex_attempts", 3)):
                    if (
                        self.stop_event.is_set()
                        or supervisor.cancelled.is_set()
                        or supervisor.closed
                    ):
                        break
                    try:
                        outcome = self.driver.run(
                            supervisor,
                            service,
                            recovery="Continue from actual records; do not repeat successful checks."
                            if attempt
                            else "",
                        )
                        self.journal.event(
                            task["task_id"],
                            "codex_attempt",
                            {"attempt": attempt + 1, **outcome},
                        )
                    except Exception as exc:
                        self.journal.event(
                            task["task_id"],
                            "codex_error",
                            {"attempt": attempt + 1, "error": str(exc)},
                        )
                    if not supervisor.closed and attempt + 1 < self.config.get(
                        "codex_attempts", 3
                    ):
                        if self.stop_event.wait(
                            self.config.get("retry_delay_seconds", 30) * (attempt + 1)
                        ):
                            break
                supervisor.close()
                if self.stop_event.is_set() and not supervisor.closed:
                    self.journal.event(task["task_id"], "worker_restart", {})
                elif not supervisor.closed:
                    supervisor.finish(
                        "Necessary Codex work remained incomplete after bounded recovery."
                    )
        except Exception as exc:
            self.workspaces.recovered = False
            self.journal.event(
                task["task_id"], "preparation_failed", {"error": str(exc)}
            )
            if self.journal.delivery(task["task_id"]) is not None:
                # A finish reply may be followed by socket/session cleanup errors.
                # The sealed sealed delivery is immutable, including on worker shutdown.
                self.journal.event(
                    task["task_id"], "post_seal_cleanup_error", {"error": str(exc)}
                )
                return
            if self.stop_event.is_set():
                self.journal.event(task["task_id"], "worker_restart", {})
                return
            from agent_ci.delivery import EXECUTIONS_SCHEMA

            staged = run_dir / ".preparation-sealing"
            staged.mkdir(exist_ok=True)
            atomic_json(
                staged / "execution-summary.json",
                {
                    "schema": EXECUTIONS_SCHEMA,
                    "task_id": task["task_id"],
                    "run_id": row["run_id"],
                    "executions": [],
                },
            )
            result = {
                "schema": RESULT_SCHEMA,
                "task": task,
                "run_id": row["run_id"],
                "status": "cancelled" if prepare_cancel.is_set() else "infra_error",
                "summary": "Environment or worker preparation did not complete",
                "required_checks": [],
                "checks": [],
                "reviews": {},
                "findings": [],
                "blockers": [],
                "performance": [],
                "artifacts": [],
                "unfinished": ["environment preparation", str(exc)],
                "environment": {},
                "execution_summary_sha256": hashlib.sha256(
                    (staged / "execution-summary.json").read_bytes()
                ).hexdigest(),
            }
            atomic_json(staged / "result.json", result)
            sealed = run_dir / "sealed"
            os.replace(staged, sealed)
            self.journal.queue_result(
                task["task_id"],
                sealed / "result.json",
                hashlib.sha256((sealed / "result.json").read_bytes()).hexdigest(),
            )
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=5)
            if supervisor:
                supervisor.close()
            self.active = None
            self.manager.cancel_event = None
            if generation is not None:
                if executor is None:
                    executor = self.make_executor(generation, task)
                self.workspaces.finish(executor)
            self.heartbeat()

    def deliver(self, row):
        task = json.loads(row["manifest"])
        if self.journal.task(task["task_id"])["run_id"] != row["run_id"]:
            return  # A stale caller must not upload the explicitly resumed run.
        box = self.journal.delivery(task["task_id"])
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
        if (
            result.get("task") != task
            or result.get("run_id") != row["run_id"]
            or result.get("schema") != RESULT_SCHEMA
        ):
            raise ContractError(
                "Sealed result identity differs from the pending task/run"
            )
        self.relay.refresh()
        # A sealed obsolete run remains useful history. The receiver prevents it
        # from changing the current PR; publication does not need a live Agent.
        uploaded_digest = self.relay.publish_result(
            task, row["run_id"], result_path.parent
        )
        if uploaded_digest != box["digest"]:
            raise ContractError(
                "Uploaded result digest differs from sealed sealed delivery"
            )
        self.journal.published(task["task_id"])

    def retry_delivery(self, row):
        """One durable upload attempt. Never invokes Codex or task tools."""
        try:
            self.deliver(row)
        except Exception as exc:
            count = self.journal.publication_failure(row["task_id"])
            detail = {
                "upload_error": str(exc),
                "attempts": count,
                "reason": "retry_saved_upload",
            }
            self.journal.event(row["task_id"], "delivery_error", detail)
            self.journal.phase(row["task_id"], "publish_pending", detail)

    def retry_optional_delivery(self):
        """One due published run per scan; reuse its files without starting tools."""
        now = time.time()
        interval = max(60, self.config.get("optional_delivery_retry_seconds", 300))
        lifetime = self.config.get("results_retention_days", 30) * 86400
        candidates = []
        for path in (self.state_dir / "runs").glob("*/*/state.json"):
            state = json.loads(path.read_bytes())
            delivery = state.get("delivery") or {}
            published = delivery.get("published")
            if (
                state.get("phase") != "published"
                or not published
                or now >= published + lifetime
                or now < delivery.get("optional_retry_after", 0)
            ):
                continue
            index_path = path.parent / "delivery-index.json"
            if not index_path.is_file():
                continue
            index = json.loads(index_path.read_bytes())
            if (
                index.get("status") == "ready"
                and index.get("result_digest") == delivery.get("digest")
                and any(
                    row.get("required") is False and row.get("status") == "pending"
                    for row in index.get("artifacts", [])
                )
            ):
                candidates.append((delivery.get("optional_retry_after", 0), path))
        if not candidates:
            return
        _, path = min(candidates)
        # Record the cadence in the existing run state, including historical
        # published runs which are no longer Journal.task()'s latest run.
        with self.journal.guard:
            state = json.loads(path.read_bytes())
            delivery = state["delivery"]
            delivery["optional_retry_after"] = now + interval
            delivery["optional_attempts"] = delivery.get("optional_attempts", 0) + 1
            self.journal._write(state["task_id"], state)
        try:
            result_path = Path(delivery["payload_path"])
            payload = result_path.read_bytes()
            result = json.loads(payload)
            if (
                hashlib.sha256(payload).hexdigest() != delivery["digest"]
                or result.get("task", {}).get("task_id") != state["task_id"]
                or result.get("run_id") != state["run_id"]
            ):
                raise ContractError("Optional delivery differs from sealed task/run")
            uploaded = self.relay.publish_result(
                result["task"], state["run_id"], result_path.parent, optional_only=True
            )
            if uploaded != delivery["digest"]:
                raise ContractError(
                    "Optional delivery changed the sealed result digest"
                )
        except Exception as exc:
            # Necessary evidence was already delivered. An optional attachment
            # outage never reopens the run or changes the sealed test outcome.
            with self.journal.guard:
                current = json.loads(path.read_bytes())
                current["events"] = (
                    current.get("events", [])
                    + [
                        {
                            "at": now,
                            "kind": "optional_delivery_error",
                            "detail": {"error": str(exc)},
                        }
                    ]
                )[-100:]
                self.journal._write(current["task_id"], current)

    def scan(self):
        # Sealed delivery must not depend on a working container daemon, image
        # registry, free build space, or a recoverable task volume.
        attempted = set()
        for row in self.journal.tasks():
            if row["phase"] == "publish_pending":
                self.retry_delivery(row)
                attempted.add(row["task_id"])
        self.retry_optional_delivery()
        try:
            # Recover failed trusted image-validation containers independently
            # of rotate(), whose safety gate deliberately blocks new builds.
            self.manager.collect_retired()
            self.workspaces.recover()
            maintenance = self.workspaces.collect()
        except Exception as exc:
            self.heartbeat(runtime="unavailable", runtime_error=str(exc))
            return
        if maintenance["status"] != "healthy":
            self.heartbeat(runtime="maintenance_blocked")
            return
        self.relay.refresh()
        self.workspaces.recover()
        self.workspaces.collect()
        for task in self.relay.tasks():
            if self.stop_event.is_set():
                break
            try:
                self.process(task)
                row = self.journal.task(task["task_id"])
                if (
                    row["phase"] == "publish_pending"
                    and row["task_id"] not in attempted
                ):
                    self.retry_delivery(row)
                    attempted.add(row["task_id"])
            except Exception as exc:
                self.journal.event(
                    task.get("task_id", "invalid"), "task_error", {"error": str(exc)}
                )
        self.heartbeat()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.environ.get("LOCAL_CI_CONFIG_JSON", "/opt/local-ci/config.json"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resume", metavar="TASK_ID")
    args = parser.parse_args(argv)
    worker = Worker(json.loads(Path(args.config).read_text()))
    import fcntl

    with (worker.state_dir / "poll.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another worker owns the poll lock", file=sys.stderr)
            return 2
        if args.resume:
            worker.journal.resume(args.resume)
            return 0

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
