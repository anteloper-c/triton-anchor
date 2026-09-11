#!/usr/bin/env python3
"""Independently collect and optionally publish worker health to Gitee."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))
from prepare.runtime import EnvironmentManager
from prepare.artifacts import atomic_json, safe_source
from prepare.runtime_probe import runtime_status


def iso(value: float | None = None) -> str:
    return datetime.fromtimestamp(
        time.time() if value is None else value, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    stale = not heartbeat or now - heartbeat > int(
        config.get("heartbeat_stale_seconds", 180)
    )
    tasks, uploads = [], []
    for file in sorted((state / "runs").glob("*/*/state.json")):
        try:
            record = json.loads(file.read_text())
            if record.get("abandoned") or record.get("phase") == "published":
                continue
            task = {
                "task_id": file.parent.parent.name,
                "run_id": file.parent.name,
                "stage": record.get("phase", "preparing"),
                "updated_at": iso(record["updated"])
                if isinstance(record.get("updated"), (int, float))
                else record.get("updated_at"),
            }
            tasks.append(task)
            if task["stage"] == "publish_pending":
                uploads.append(
                    {
                        "task_id": task["task_id"],
                        "queued_at": task["updated_at"],
                        "attempts": (record.get("delivery") or {}).get("attempts", 0),
                    }
                )
        except (OSError, ValueError):
            continue
    active = next(
        (entry for entry in tasks if entry["stage"] == "running"),
        tasks[0] if tasks else None,
    )
    if active and active["stage"] == "running":
        for key in ("last_progress_at", "codex_alive"):
            if key in worker:
                value = worker[key]
                active[key] = (
                    iso(value)
                    if key.endswith("_at") and isinstance(value, (int, float))
                    else value
                )
    roots = {str(state)}
    storage = []
    for root in sorted(roots):
        entry = {
            "label": "state" if root == str(state) else "environment",
            "available": Path(root).exists(),
        }
        if entry["available"]:
            usage = shutil.disk_usage(root)
            entry.update(
                filesystem_total_bytes=usage.total,
                filesystem_free_bytes=usage.free,
                filesystem_used_percent=round(100 * usage.used / usage.total, 2)
                if usage.total
                else 100,
            )
        storage.append(entry)
    try:
        environments = (manager or EnvironmentManager(config, state)).health()
    except Exception as exc:
        environments = {
            "active_images": {},
            "images": [],
            "attempts": [],
            "error": type(exc).__name__,
        }
    runtime = dict(environments.get("runtime", {}))
    if config.get("runtime", {}).get("kind") == "docker-rootless":
        try:
            runtime_status(config)
            runtime.update(kind="docker-rootless", available=True, rootless=True)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            runtime.update(
                kind="docker-rootless", available=False, error=type(exc).__name__
            )
    if runtime:
        environments = {**environments, "runtime": runtime}
        if runtime.get("available") is False:
            environments.setdefault("error", "RootlessRuntimeUnavailable")
    try:
        retention = json.loads((state / "health/retention.json").read_text())
    except FileNotFoundError:
        retention = {}
    except (OSError, ValueError):
        retention = {"errors": ["unreadable"]}
    cleanup_failed = any(
        row.get("state") == "unsafe" for row in environments.get("attempts", [])
    )
    workspaces = {
        "status": "error" if cleanup_failed or retention.get("errors") else "healthy",
        "pause_intake": retention.get("pause_intake") is True,
    }
    services = []
    for name in config.get(
        "monitor_services",
        ["triton-anchor-local-ci.service", "triton-anchor-local-ci-health.timer"],
    ):
        if not isinstance(name, str) or not __import__("re").fullmatch(
            r"[A-Za-z0-9_.@-]+\.(service|timer)", name
        ):
            raise ValueError("monitor_services contains an invalid systemd unit name")
        row = {"name": name, "available": False}
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    name,
                    "--property=LoadState,ActiveState,SubState,Result",
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )
            fields = dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )
            row.update(
                available=result.returncode == 0
                and fields.get("LoadState") == "loaded",
                active_state=fields.get("ActiveState", "unknown"),
                sub_state=fields.get("SubState", "unknown"),
                result=fields.get("Result", "unknown"),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        services.append(row)
    snapshot = {
        "schema": "triton-anchor-worker-health",
        "worker_id": config.get("worker_id", "local-ci"),
        "collected_at": iso(now),
        "state": "offline" if not alive or stale else "busy" if active else "healthy",
        "poller": {
            "alive": alive,
            "heartbeat_at": iso(heartbeat) if heartbeat else None,
            "heartbeat_stale": stale,
            "last_poll_status": "error"
            if worker.get("control_channel") == "unreachable"
            else "success",
        },
        "active_task": active,
        "tasks": tasks,
        "uploads": uploads,
        "environments": environments,
        "workspaces": workspaces,
        "storage": storage,
        "services": services,
        "service_scope": "user",
        "runtime": runtime,
        "images": environments.get("images", []),
        "task_containers": environments.get("attempts", []),
    }
    return public_snapshot(snapshot)


def public_snapshot(snapshot):
    """Construct the public schema; never serialize manager/config structures."""

    def identifier(value):
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
            else "unknown"
        )

    def instant(value):
        try:
            return (
                value
                if isinstance(value, str)
                and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo
                else None
            )
        except ValueError:
            return None

    def number(value):
        return (
            value
            if type(value) in (int, float)
            and __import__("math").isfinite(value)
            and value >= 0
            else None
        )

    def task(row):
        return {
            "task_id": identifier(row.get("task_id")),
            "run_id": identifier(row.get("run_id")),
            "stage": row.get("stage")
            if row.get("stage")
            in {"preparing", "running", "sealing", "publish_pending", "published"}
            else "unknown",
            "updated_at": instant(row.get("updated_at")),
            "last_progress_at": instant(row.get("last_progress_at")),
            "codex_alive": row.get("codex_alive")
            if type(row.get("codex_alive")) is bool
            else None,
        }

    poller = snapshot.get("poller", {})
    runtime = snapshot.get("runtime", {})
    environment = snapshot.get("environments", {})
    result = {
        "schema": "triton-anchor-worker-health",
        "worker_id": identifier(snapshot.get("worker_id")),
        "collected_at": instant(snapshot.get("collected_at")),
        "service_scope": "user",
        "state": snapshot.get("state")
        if snapshot.get("state") in {"healthy", "busy", "offline"}
        else "unknown",
        "poller": {
            "alive": poller.get("alive") is True,
            "heartbeat_at": instant(poller.get("heartbeat_at")),
            "heartbeat_stale": poller.get("heartbeat_stale") is True,
            "last_poll_status": "error"
            if poller.get("last_poll_status") == "error"
            else "success",
        },
        "runtime": {
            "kind": "docker-rootless",
            "available": runtime.get("available") is True,
            "rootless": runtime.get("rootless") is True,
        },
        "environments": {
            "unavailable": bool(
                environment.get("error") or environment.get("unavailable")
            )
        },
        "resource_usage": [
            {k: number(r.get(k)) for k in ("cpu_percent", "memory_percent", "pids")}
            for r in environment.get(
                "resource_usage", snapshot.get("resource_usage", [])
            )
            if isinstance(r, dict)
        ],
        "resource_usage_available": environment.get(
            "resource_usage_available", snapshot.get("resource_usage_available")
        )
        is True,
        "workspaces": {
            "status": snapshot.get("workspaces", {}).get("status")
            if snapshot.get("workspaces", {}).get("status")
            in {"healthy", "error", "unreported"}
            else "unknown",
            "pause_intake": snapshot.get("workspaces", {}).get("pause_intake") is True,
        },
        "tasks": [task(r) for r in snapshot.get("tasks", []) if isinstance(r, dict)],
        "active_task": task(snapshot["active_task"])
        if isinstance(snapshot.get("active_task"), dict)
        else None,
        "uploads": [
            {
                "task_id": identifier(r.get("task_id")),
                "queued_at": instant(r.get("queued_at")),
                "attempts": number(r.get("attempts")),
            }
            for r in snapshot.get("uploads", [])
            if isinstance(r, dict)
        ],
        "storage": [
            {
                k: number(r.get(k))
                for k in (
                    "filesystem_total_bytes",
                    "filesystem_free_bytes",
                    "filesystem_used_percent",
                )
            }
            for r in snapshot.get("storage", [])
            if isinstance(r, dict)
        ],
        "images": [
            {
                "release_id": identifier(r.get("release_id")),
                "image_id": identifier(r.get("image_id")),
                "state": r.get("state")
                if r.get("state")
                in {"preparing", "validating", "ready", "failed", "quarantined"}
                else "unknown",
                "validated": r.get("validated") is True,
            }
            for r in snapshot.get("images", [])
            if isinstance(r, dict)
        ],
        "services": [
            {
                "name": identifier(r.get("name")),
                "available": r.get("available") is True,
                "active_state": r.get("active_state")
                if r.get("active_state")
                in {"active", "inactive", "failed", "activating"}
                else "unknown",
            }
            for r in snapshot.get("services", [])
            if isinstance(r, dict)
        ],
    }
    return result


def publish_snapshot(config, document, *, branch, filename):
    """Single-writer, parentless snapshot with exact old-SHA compare-and-swap."""
    from urllib.parse import urlparse

    repository = safe_source(config.get("health_repo_url"), "health_repo_url")
    parsed = urlparse(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gitee.com"
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Health publication requires credential-free HTTPS Gitee URL")
    if not re.fullmatch(r"(?:snapshot/[A-Za-z0-9_.-]+|watchdog)", branch):
        raise ValueError("Only dedicated health snapshot refs may be replaced")
    token = os.environ.get(config.get("health_token_env", "GITEE_HEALTH_TOKEN"), "")
    if not token:
        raise ValueError("Health publishing credential is unavailable")
    with tempfile.TemporaryDirectory(prefix="local-ci-health-") as temporary:
        root = Path(temporary)
        askpass = root / "askpass.sh"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$LOCAL_CI_GIT_USER" ;; *) printf "%s\\n" "$LOCAL_CI_GIT_TOKEN" ;; esac\n'
        )
        askpass.chmod(0o700)
        env = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "LOCAL_CI_GIT_TOKEN": token,
            "LOCAL_CI_GIT_USER": config.get("gitee_username", "oauth2"),
        }
        checkout = root / "repo"
        checkout.mkdir()

        def git(*args):
            result = subprocess.run(
                ["git", *args],
                cwd=checkout,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError("Gitee health snapshot operation failed")
            return result.stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Local CI Health")
        git("config", "user.email", "local-ci-health@example.invalid")
        git("remote", "add", "origin", repository)
        ref = "refs/heads/" + branch
        existing = git("ls-remote", "origin", ref)
        old = existing.split()[0] if existing else ""
        atomic_json(checkout / filename, document)
        git("add", "--", filename)
        tree = git("write-tree")
        commit = git("commit-tree", tree, "-m", "Current Local CI health snapshot")
        git(
            "push",
            "--force-with-lease=" + ref + ":" + old,
            "origin",
            commit + ":" + ref,
        )


def publish(config, snapshot):
    publish_snapshot(
        config,
        public_snapshot(snapshot),
        branch="snapshot/" + config.get("worker_id", "local-ci"),
        filename="worker-health.json",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        snapshot = collect(config)
        output = (
            Path(args.output)
            if args.output
            else Path(config["state_dir"]) / "health/worker-health.json"
        )
        atomic_json(output, snapshot)
        if args.publish:
            publish(config, snapshot)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Local CI health collection/publishing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
