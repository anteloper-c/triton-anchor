#!/usr/bin/env python3
"""Independently collect and optionally publish worker health to Gitee."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))
from environments.manager import EnvironmentManager, atomic_json, safe_source
from deploy.runtime_probe import runtime_status


def iso(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(config: dict, *, now: float | None = None, manager=None) -> dict:
    now = time.time() if now is None else now
    state = Path(config["state_dir"])
    path = state / "health/worker.json"
    worker = {}
    try:
        worker = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    heartbeat = worker.get("heartbeat_at", 0)
    heartbeat = heartbeat if isinstance(heartbeat, (int, float)) else 0
    pid = worker.get("pid", 0)
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    stale = not heartbeat or now - heartbeat > int(config.get("heartbeat_stale_seconds", 180))
    tasks, uploads = [], []
    database = state / "journal.sqlite3"
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute("SELECT task_id,run_id,phase,updated FROM tasks WHERE phase NOT IN ('complete','cancelled') ORDER BY updated"):
                task = {"task_id": row["task_id"], "run_id": row["run_id"], "stage": row["phase"], "updated_at": iso(row["updated"])}
                tasks.append(task)
            for row in connection.execute("""SELECT outbox.task_id,attempts,tasks.updated,
                    (SELECT MIN(at) FROM events WHERE events.task_id=outbox.task_id AND kind='phase:publishing'
                     AND at >= COALESCE((SELECT MAX(at) FROM events AS resumed
                                         WHERE resumed.task_id=outbox.task_id AND resumed.kind='explicit_resume'), 0)) AS queued
                    FROM outbox JOIN tasks USING(task_id) WHERE published IS NULL AND phase='publishing'"""):
                uploads.append({"worker_id": config.get("worker_id", "local-ci"), "task_id": row["task_id"], "state": "pending_upload",
                                "queued_at": iso(row["queued"] or row["updated"]), "attempts": row["attempts"]})
    active = next((entry for entry in tasks if entry["stage"] == "running"), tasks[0] if tasks else None)
    if active and active["stage"] == "running":
        for key in ("last_progress_at", "codex_alive"):
            if key in worker:
                value = worker[key]
                active[key] = iso(value) if key.endswith("_at") and isinstance(value, (int, float)) else value
    roots = {str(state)}
    storage = []
    for root in sorted(roots):
        entry = {"label": "state" if root == str(state) else "environment", "available": Path(root).exists()}
        if entry["available"]:
            usage = shutil.disk_usage(root)
            entry.update(filesystem_total_bytes=usage.total, filesystem_free_bytes=usage.free,
                         filesystem_used_percent=round(100 * usage.used / usage.total, 2) if usage.total else 100)
        storage.append(entry)
    try:
        environments = (manager or EnvironmentManager(config, state)).health()
    except Exception as exc:
        environments = {"active_images": {}, "images": [], "attempts": [], "error": type(exc).__name__}
    runtime = dict(environments.get("runtime", {}))
    if config.get("runtime", {}).get("kind") == "docker-rootless":
        try:
            runtime_status(config)
            runtime.update(kind="docker-rootless", available=True, rootless=True)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            runtime.update(kind="docker-rootless", available=False, error=type(exc).__name__)
    if runtime:
        environments = {**environments, "runtime": runtime}
        if runtime.get("available") is False:
            environments.setdefault("error", "RootlessRuntimeUnavailable")
    try:
        workspaces = json.loads((state / "workspace-health.json").read_text())
        if not isinstance(workspaces, dict):
            raise ValueError("Expected a workspace health object")
    except FileNotFoundError:
        workspaces = {"status": "unreported", "reason": "No task workspace cleanup snapshot has been recorded"}
    except (OSError, ValueError) as exc:
        workspaces = {"status": "error", "error": type(exc).__name__, "reason": "Task workspace cleanup snapshot is unreadable"}
    services = []
    for name in config.get("monitor_services", ["triton-anchor-local-ci.service", "triton-anchor-local-ci-health.timer"]):
        if not isinstance(name, str) or not __import__("re").fullmatch(r"[A-Za-z0-9_.@-]+\.(service|timer)", name):
            raise ValueError("monitor_services contains an invalid systemd unit name")
        row = {"name": name, "available": False}
        try:
            result = subprocess.run(["systemctl", "--user", "show", name, "--property=LoadState,ActiveState,SubState,Result"], text=True, capture_output=True, timeout=5)
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            row.update(available=result.returncode == 0 and fields.get("LoadState") == "loaded",
                       active_state=fields.get("ActiveState", "unknown"), sub_state=fields.get("SubState", "unknown"),
                       result=fields.get("Result", "unknown"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        services.append(row)
    snapshot = {"schema": "triton-anchor-worker-health/v4", "worker_id": config.get("worker_id", "local-ci"), "collected_at": iso(now),
                "state": "offline" if not alive or stale else "busy" if active else "healthy",
                "poller": {"alive": alive, "heartbeat_at": iso(heartbeat) if heartbeat else None, "heartbeat_stale": stale,
                           "last_poll_status": "error" if worker.get("control_channel") == "unreachable" else "success"},
                "active_task": active, "tasks": tasks, "uploads": uploads, "environments": environments,
                "workspaces": workspaces, "storage": storage, "services": services, "service_scope": "user",
                "runtime": runtime, "images": environments.get("images", []),
                "task_containers": environments.get("attempts", [])}
    return snapshot


def publish(config: dict, snapshot: dict) -> None:
    repository = safe_source(config.get("health_repo_url"), "health_repo_url")
    branch = config.get("health_branch", "main")
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ValueError("health_branch must be a configured branch")
    token = os.environ.get(config.get("health_token_env", "GITEE_HEALTH_TOKEN"), "")
    if not token:
        raise ValueError("Health publishing token environment variable is missing")
    with tempfile.TemporaryDirectory(prefix="local-ci-health-publish-") as temporary:
        root = Path(temporary)
        askpass = root / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$LOCAL_CI_GIT_USER" ;; *) printf "%s\\n" "$LOCAL_CI_GIT_TOKEN" ;; esac\n')
        askpass.chmod(0o700)
        env = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "LOCAL_CI_GIT_TOKEN": token,
               "LOCAL_CI_GIT_USER": config.get("gitee_username", "oauth2")}
        checkout = root / "repo"
        def git(*args):
            result = subprocess.run(["git", *args], cwd=checkout if checkout.exists() else root, env=env, text=True, capture_output=True, timeout=90)
            if result.returncode:
                raise RuntimeError("Health repository operation failed; inspect authentication, repository and branch configuration")
            return result.stdout
        git("clone", "--single-branch", "--branch", branch, "--", repository, str(checkout))
        git("config", "user.name", "Local CI Health")
        git("config", "user.email", "local-ci-health@example.invalid")
        atomic_json(checkout / "worker-health.json", snapshot)
        git("add", "--", "worker-health.json")
        if not git("diff", "--cached", "--name-only").strip():
            return
        git("commit", "-m", "Update Local CI worker health")
        for attempt in range(3):
            try:
                git("push", "origin", f"HEAD:refs/heads/{branch}")
                return
            except RuntimeError:
                if attempt == 2:
                    raise
                git("fetch", "origin", branch)
                git("rebase", f"origin/{branch}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        snapshot = collect(config)
        output = Path(args.output) if args.output else Path(config["state_dir"]) / "health/worker-health.json"
        atomic_json(output, snapshot)
        if args.publish:
            publish(config, snapshot)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Local CI health collection/publishing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
