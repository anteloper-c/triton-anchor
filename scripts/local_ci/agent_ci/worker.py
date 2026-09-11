#!/usr/bin/env python3
"""Poll Gitee, prepare a task for Codex, then publish its selected results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import sys
import threading
import time
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))

from agent_ci.codex import CodexDriver
from agent_ci.delivery import seal_result
from agent_ci.executor import DockerExecutor
from agent_ci.policy import changed_files, minimum_checks
from agent_ci.protocol import ContractError, atomic_json, is_legacy_task, validate_task
from agent_ci.relay import GitRelay
from agent_ci.state import Journal


class ActiveTask:
    def __init__(self, task, manager):
        self.task, self.manager = task, manager
        self.cancelled = threading.Event()
        self.generation = None
        self.reason = ""

    def cancel(self, reason):
        self.reason = reason
        self.cancelled.set()
        if self.generation is not None:
            self.manager.stop_task(self.generation)


class Worker:
    def __init__(
        self, config, *, relay=None, manager=None, driver=None, executor_factory=None
    ):
        self.config, self.state_dir = config, Path(config["state_dir"])
        self.journal = Journal(self.state_dir)
        self.relay = relay or GitRelay(
            config["gitee_repo_url"],
            self.state_dir / "relay",
            allow_local=config.get("simulation", False),
        )
        if manager is None:
            from prepare.runtime import EnvironmentManager

            manager = EnvironmentManager(config, self.state_dir)
        self.manager = manager
        self.driver = driver or CodexDriver(config, self.state_dir)
        self.executor_factory = executor_factory or DockerExecutor
        self.stop_event = threading.Event()
        self.active = None

    def heartbeat(self, **extra):
        atomic_json(
            self.state_dir / "health/worker.json",
            {
                "schema": "triton-anchor-worker-health",
                "worker_id": self.config.get("worker_id", "local-ci"),
                "heartbeat_at": time.time(),
                "pid": os.getpid(),
                "tasks": [
                    {k: r[k] for k in ("task_id", "phase", "updated")}
                    for r in self.journal.tasks()
                ],
                **extra,
            },
        )

    def watch(self, active, done):
        while not done.wait(self.config.get("poll_interval_seconds", 60)):
            try:
                self.relay.refresh()
                valid, reason = self.relay.validity(active.task)
                if not valid:
                    active.cancel(reason)
                self.heartbeat(active_task=active.task["task_id"])
            except Exception as exc:
                self.journal.event(
                    active.task["task_id"], "poll_error", {"error": str(exc)}
                )
                self.heartbeat(control_channel="unreachable")

    def queue_sealed(self, task_id, path):
        self.journal.queue_result(
            task_id, path, hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def process(self, task):
        if is_legacy_task(task):
            return
        validate_task(
            task,
            tuple(self.config.get("repositories", ["likehupochuan/triton-anchor"])),
        )
        valid, _ = self.relay.validity(task)
        if not valid:
            return
        row = self.journal.register(task)
        if row["phase"] in {"publish_pending", "published"}:
            return
        run_dir = self.journal.run_dir(task["task_id"])
        sealed = run_dir / "sealed" / "result.json"
        if sealed.is_file():
            self.queue_sealed(task["task_id"], sealed)
            return
        if row["phase"] == "running" or json.loads(row["detail"]).get("started"):
            row = self.journal.restart(task["task_id"])
            run_dir = self.journal.run_dir(task["task_id"])
        self.journal.phase(task["task_id"], "preparing", {"started": True})
        active = ActiveTask(task, self.manager)
        self.active = active
        self.manager.cancel_event = active.cancelled
        done = threading.Event()
        watcher = threading.Thread(target=self.watch, args=(active, done), daemon=True)
        watcher.start()
        generation = None
        policy, environment = {}, {}
        report = {"status": "infra_error", "summary": "任务未完成"}
        try:
            generation = self.manager.acquire_task(task, row["run_id"])
            active.generation = generation
            if active.cancelled.is_set():
                raise InterruptedError(active.reason)
            executor = self.executor_factory(
                self.config,
                self.state_dir,
                generation,
                task,
                self.relay,
                manager=self.manager,
            )
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
            environment = {
                key: generation[key]
                for key in (
                    "profile",
                    "llvm_hash",
                    "backend_enabled",
                    "environment_fingerprint",
                    "image_id",
                )
            }
            # Both source identities are available offline; Codex chooses whether to build a baseline.
            executor.prepare("base")
            executor.write_context(policy, changes)
            self.journal.phase(task["task_id"], "running")
            deadline = time.monotonic() + self.config.get(
                "codex_timeout_seconds", 21600
            )
            completed = False
            for attempt in range(self.config.get("codex_attempts", 3)):
                if active.cancelled.is_set():
                    break
                try:
                    outcome = self.driver.run(
                        executor,
                        cancelled=active.cancelled,
                        deadline=deadline,
                        recovery="Resume the task. Inspect the running processes, saved plan and existing results before retrying work."
                        if attempt
                        else "",
                    )
                    self.journal.event(task["task_id"], "codex_exit", outcome)
                    if outcome["reason"] == "timeout":
                        raise TimeoutError("Codex task time budget exhausted")
                    if (
                        outcome["exit_code"] == 0
                        and (run_dir / "artifacts/agent-result.json").is_file()
                    ):
                        completed = True
                        break
                except TimeoutError:
                    raise
                except Exception as exc:
                    self.journal.event(
                        task["task_id"], "codex_error", {"error": str(exc)}
                    )
                if attempt + 1 < self.config.get("codex_attempts", 3):
                    active.cancelled.wait(self.config.get("retry_delay_seconds", 30))
            if not completed:
                raise ContractError(
                    "Codex未完成任务或未生成最终结果；请查看本机Codex日志"
                )
            report = None
            self.relay.refresh()
            valid, reason = self.relay.validity(task)
            if not valid:
                active.cancel(reason)
        except Exception as exc:
            report = {"status": "infra_error", "summary": str(exc)}
            self.journal.event(task["task_id"], "task_error", {"error": str(exc)})
        finally:
            done.set()
            watcher.join(timeout=5)
            self.manager.cancel_event = None
            try:
                if generation is not None:
                    self.manager.stop_task(generation)
                    self.manager.collect_artifacts(generation)
                    self.manager.destroy_task(generation)
            finally:
                shutil.rmtree(run_dir / "inputs", ignore_errors=True)
                self.active = None
        # Shutdown leaves an interrupted run for the next Worker start, not a PR failure.
        if self.stop_event.is_set():
            return
        if active.cancelled.is_set():
            report = {"status": "cancelled", "summary": active.reason}
        elif report is None:
            try:
                report = json.loads(
                    (run_dir / "artifacts/agent-result.json").read_text()
                )
            except (ValueError, OSError) as exc:
                report = {
                    "status": "infra_error",
                    "summary": "Codex结果无法读取：" + str(exc),
                }
        try:
            seal_result(
                task,
                row["run_id"],
                report,
                policy,
                environment,
                run_dir,
                run_dir / "sealed",
                redact=self.driver.redact,
            )
        except (ValueError, OSError, TypeError) as exc:
            seal_result(
                task,
                row["run_id"],
                {"status": "infra_error", "summary": "无法完成结果汇总：" + str(exc)},
                policy,
                environment,
                run_dir,
                run_dir / "sealed",
                redact=self.driver.redact,
            )
        self.queue_sealed(task["task_id"], run_dir / "sealed/result.json")
        self.heartbeat()

    def retry_delivery(self, row):
        box = self.journal.delivery(row["task_id"])
        path = Path(box["payload_path"])
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != box["digest"]:
                raise ContractError("Saved result changed before publication")
            task = json.loads(row["manifest"])
            digest = self.relay.publish_result(task, row["run_id"], path.parent)
            if digest != box["digest"]:
                raise ContractError("Published result differs from saved result")
            self.journal.published(row["task_id"])
        except Exception as exc:
            self.journal.publication_failure(row["task_id"])
            self.journal.event(row["task_id"], "publication_error", {"error": str(exc)})

    def scan(self):
        # Publication retries do not depend on Docker or rebuild the tested code.
        attempted = set()
        for row in self.journal.tasks():
            task = json.loads(row["manifest"])
            if not is_legacy_task(task) and row["phase"] == "publish_pending":
                self.retry_delivery(row)
                attempted.add(row["task_id"])
        for handle in self.manager.generations().values():
            if handle["state"] != "removed":
                self.manager.destroy_task(handle)
        self.manager.collect_retired()
        from maintenance.retention import retain_local

        if retain_local(self.config)["pause_intake"]:
            self.heartbeat(runtime="disk_budget_exceeded")
            return
        self.relay.refresh()
        for task in self.relay.tasks():
            if self.stop_event.is_set():
                break
            if is_legacy_task(task):
                continue
            try:
                self.process(task)
                if not any(
                    (self.state_dir / "runs" / task["task_id"]).glob("*/state.json")
                ):
                    continue
                row = self.journal.task(task["task_id"])
                if (
                    row["phase"] == "publish_pending"
                    and row["task_id"] not in attempted
                ):
                    self.retry_delivery(row)
            except Exception as exc:
                self.journal.event(
                    task.get("task_id", "invalid"), "task_error", {"error": str(exc)}
                )
                self.heartbeat(error=str(exc))
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
                worker.heartbeat(error=str(exc))
            if args.once:
                break
            worker.stop_event.wait(worker.config.get("poll_interval_seconds", 60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
