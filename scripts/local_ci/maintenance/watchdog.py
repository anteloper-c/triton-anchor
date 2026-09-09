#!/usr/bin/env python3
"""External Local CI watchdog: health/queue JSON to durable incidents and mail.

This runs independently of the worker (for example in GitHub Actions). It never
uses a model. --mail-outbox simulates SMTP delivery with reviewable .eml files;
--dry-run performs neither state writes nor delivery.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import smtplib
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any


SCHEMA = "triton-anchor-local-ci-incidents/v1"
PUBLIC_REASONS = {"sealed_success", "cancelled", "retention_expired", "disk_budget", "execution_finished",
                  "worker_recovery", "worker_shutdown", "cleanup_failed", "cleanup_timeout", "task_cleanup_failed",
                  "task_process_stop_failed", "container_identity_mismatch", "generation_missing_clean_baseline",
                  "reuse_validation_failed", "shared_dependency_or_public_state_changed",
                  "post_task_device_validation_failed", "post_validation_changed_shared_or_public_state"}


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age(now: datetime, value: Any) -> float | None:
    parsed = timestamp(value)
    return (now - parsed).total_seconds() if parsed else None


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_worker_health(worker: dict[str, Any]) -> dict[str, Any]:
    """Dashboard summary only: never copy host paths, env/config or raw errors."""
    def identifier(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else "unknown"
    def reason(value):
        return value if isinstance(value, str) and value in PUBLIC_REASONS else "details_available_on_worker"
    def rows(value):
        return value if isinstance(value, list) else []
    def label(value, choices):
        return value if isinstance(value, str) and value in choices else "unknown"
    summary = {"worker_id": identifier(worker["worker_id"]), "collected_at": worker.get("collected_at") if timestamp(worker.get("collected_at")) else None}
    workspaces = worker.get("workspaces")
    if isinstance(workspaces, dict):
        public = {"status": label(workspaces.get("status"), {"healthy", "error", "unreported"})}
        for name in ("logical_bytes", "max_bytes", "retention_hours", "state_free_bytes", "minimum_free_bytes", "durable_evidence_bytes"):
            value = workspaces.get(name)
            if type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value)):
                public[name] = value
        public["workspaces"] = [{"task_id": identifier(row.get("task_id")), "generation": identifier(row.get("generation")),
                                 "phase": label(row.get("phase"), {"active", "unsafe", "cleaning", "retained", "cleanup_failed", "removed"}),
                                 "reason": reason(row.get("reason"))}
                                for row in rows(workspaces.get("workspaces")) if isinstance(row, dict)]
        public["errors"] = [{"task_id": identifier(row.get("task_id")), "reason": reason(row.get("reason", row.get("error")))}
                            for row in rows(workspaces.get("errors")) if isinstance(row, dict)]
        summary["workspaces"] = public
    environments = worker.get("environments")
    if isinstance(environments, dict):
        public = {"unavailable": bool(environments.get("error")), "generations": []}
        for row in rows(environments.get("generations")):
            if not isinstance(row, dict):
                continue
            generation = {"generation": identifier(row.get("generation")),
                          "state": label(row.get("state"), {"preparing", "active", "previous", "ready", "retired", "failed", "dirty", "quarantined"})}
            for name in ("active", "running", "stopped", "reusable", "leased"):
                if type(row.get(name)) is bool:
                    generation[name] = row[name]
            if row.get("quarantine_reason"):
                generation["quarantine_reason"] = reason(row["quarantine_reason"])
            public["generations"].append(generation)
        summary["environments"] = public
    runtime = worker.get("runtime", environments.get("runtime", {}) if isinstance(environments, dict) else {})
    if isinstance(runtime, dict):
        summary["runtime"] = {"kind": "docker-rootless" if runtime.get("kind") == "docker-rootless" else "unknown"}
        for name in ("available", "rootless"):
            if type(runtime.get(name)) is bool:
                summary["runtime"][name] = runtime[name]
        if runtime.get("error"):
            summary["runtime"]["unavailable"] = True
    images = worker.get("images", environments.get("images", []) if isinstance(environments, dict) else [])
    summary["images"] = [{"release_id": identifier(row.get("release_id")), "image_id": identifier(row.get("image_id")),
                          "state": label(row.get("state"), {"preparing", "active", "ready", "previous", "retired", "failed"}),
                          "validated": row.get("validated") is True}
                         for row in rows(images) if isinstance(row, dict)]
    attempts = worker.get("task_containers", environments.get("attempts", []) if isinstance(environments, dict) else [])
    summary["task_containers"] = [{"task_id": identifier(row.get("task_id")), "run_id": identifier(row.get("run_id")),
                                   "attempt_id": identifier(row.get("attempt_id", row.get("generation"))),
                                   "image_id": identifier(row.get("image_id")),
                                   "state": label(row.get("state"), {"preparing", "active", "running", "stopped", "retained", "removed", "dirty", "quarantined", "failed", "cleanup_failed"}),
                                   "stopped": row.get("stopped") is True}
                                  for row in rows(attempts) if isinstance(row, dict)]
    return summary


def evaluate(document: dict[str, Any], previous: dict[str, Any] | None = None, *, now: datetime | None = None,
             stale_seconds: int = 1200, upload_seconds: int = 1200,
             progress_seconds: int = 1800, queue_seconds: int = 3600, disk_free_bytes: int = 5 * 1024**3) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    previous = previous or {"schema": SCHEMA, "active": {}, "pending_notifications": [], "history": []}
    if previous.get("schema") != SCHEMA:
        raise ValueError("Unsupported incident state schema")
    observed: dict[str, dict[str, Any]] = {}
    unknown_workers: set[str] = set()
    unknown_incidents: set[str] = set()
    worker_health = []

    def incident(worker: str, code: str, detail: str, *, task_id: str = "", generation: str = "") -> None:
        key = ":".join(filter(None, (worker, code, task_id, generation)))
        observed[key] = {"key": key, "worker_id": worker, "code": code, "detail": detail, "task_id": task_id, "severity": "error"}
        if generation:
            observed[key]["generation"] = generation

    workers = document.get("workers")
    if workers is None:
        workers = [document] if document.get("worker_id") else []
    if not isinstance(workers, list):
        raise ValueError("workers must be a list")
    expected = document.get("expected_workers", [])
    if not isinstance(expected, list):
        raise ValueError("expected_workers must be a list")
    expected = {str(value) for value in expected}
    if document.get("source_error"):
        incident("monitor", "health_source_unavailable", "无法读取 Gitee 健康快照；检查中转仓库、网络和监控凭据。")
        unknown_workers.update(str(item.get("worker_id")) for item in previous["active"].values())
    if document.get("tasks_source_error"):
        unknown_workers.add("receiver")
        incident("monitor", "task_source_unavailable", "无法读取任务队列；检查 Gitee 中转认证和接收服务。")
    seen: set[str] = set()
    for worker in workers:
        if not isinstance(worker, dict) or not worker.get("worker_id"):
            raise ValueError("Each worker snapshot needs worker_id")
        worker_id = str(worker["worker_id"])
        if worker_id in seen:
            raise ValueError("Duplicate worker snapshot")
        seen.add(worker_id)
        worker_health.append(public_worker_health(worker))
        collected_age = age(now, worker.get("collected_at"))
        if collected_age is None or collected_age > stale_seconds or collected_age < -300:
            incident(worker_id, "worker_offline", "服务器心跳缺失或超时；检查主机、电源、网络和 systemd。")
            unknown_workers.add(worker_id)
            continue
        poller = worker.get("poller", {})
        if worker.get("state") == "offline" or poller.get("alive") is False or poller.get("heartbeat_stale") is True:
            incident(worker_id, "poller_unavailable", "Poller 未运行或心跳失效；检查 systemd 状态和 journal。")
        if poller.get("last_poll_status") == "error":
            incident(worker_id, "relay_poll_failed", "任务轮询失败；检查 Gitee 网络、认证和任务协议。")
        for service in worker.get("services", []):
            if not service.get("available") or service.get("active_state") in {"failed", "inactive"}:
                incident(worker_id, "systemd_service_unavailable", "必要的 systemd 服务或健康定时器不可用；检查 unit 的 ActiveState、Result 和 journal。")
        containers = worker.get("containers", [])
        if worker.get("container"):
            containers = [*containers, worker["container"]]
        for container in containers:
            if isinstance(container, dict) and container.get("running") is False:
                incident(worker_id, "container_unavailable", "任务所需常驻容器不可用；检查环境健康和上一可用代际。")
        environments = worker.get("environments", {})
        if not isinstance(environments, dict) or environments.get("error") or not isinstance(environments.get("generations"), list):
            unknown_incidents.update(key for key, entry in previous["active"].items()
                                     if entry.get("worker_id") == worker_id and entry.get("code") == "environment_quarantine_unconfirmed")
        if isinstance(environments, dict):
            generation_rows = environments.get("generations", [])
            active_map = environments.get("active", {})
            malformed = not isinstance(generation_rows, list) or not isinstance(active_map, dict)
            generation_rows = [entry for entry in generation_rows if isinstance(entry, dict)] if isinstance(generation_rows, list) else []
            active = set(active_map.values()) if isinstance(active_map, dict) else set()
            active_created = {entry.get("target_branch"): entry.get("created_at", "") for entry in generation_rows if entry.get("generation") in active}
            if environments.get("error") or malformed:
                incident(worker_id, "environment_state_unavailable", "无法读取环境注册表或容器状态；检查可信状态目录和 Docker 服务。")
            for generation in generation_rows:
                if generation.get("state") == "quarantined" and generation.get("stopped") is not True:
                    identifier = generation.get("generation")
                    identifier = identifier if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", identifier) else "unknown"
                    incident(worker_id, "environment_quarantine_unconfirmed", "隔离环境尚未确认停止，已阻止接单和环境切换；检查指定代际的容器身份、进程清理和 Docker 状态，勿手改 registry 放行。",
                             generation=identifier)
                if generation.get("generation") in active and not generation.get("running"):
                    incident(worker_id, "container_unavailable", "活动环境代际不可用；检查容器并按部署记录回退。")
                if generation.get("state") == "failed" and generation.get("created_at", "") >= active_created.get(generation.get("target_branch"), ""):
                    incident(worker_id, "environment_rebuild_failed", "候选环境重建或验证失败；旧环境继续服务，检查准备日志。")
        workspaces = worker.get("workspaces", {})
        if isinstance(workspaces, dict) and workspaces.get("status") == "error":
            incident(worker_id, "workspace_cleanup_failed", "任务工作目录回收失败、超出预算或可信 state 空间不足；检查 workspace-health.json 的状态与预算，保留已封存 outbox 和执行证据。")
        elif not isinstance(workspaces, dict) or workspaces.get("status") != "healthy":
            unknown_incidents.add(worker_id + ":workspace_cleanup_failed")
        for storage in worker.get("storage", []):
            free = storage.get("filesystem_free_bytes", storage.get("free_bytes"))
            used = storage.get("filesystem_used_percent", storage.get("used_percent"))
            if (isinstance(free, (int, float)) and free < disk_free_bytes) or (isinstance(used, (int, float)) and used >= 95):
                incident(worker_id, "disk_space_low", "可用磁盘不足；检查受管产物保留和环境代际，不要执行全局清理。")
        active_task = worker.get("active_task") or worker.get("task") or {}
        if active_task:
            task_id = str(active_task.get("task_id", active_task.get("run_id", "")))
            stage = active_task.get("stage", active_task.get("state", ""))
            progress = active_task.get("last_progress_at", active_task.get("heartbeat_at"))
            if stage != "publishing" and progress is not None and (age(now, progress) or 0) > progress_seconds:
                incident(worker_id, "task_no_progress", "任务长时间无有效进展；检查 Codex、工具进程、编译资源和公司模型中转。", task_id=task_id)
            if stage != "publishing" and active_task.get("codex_alive") is False:
                incident(worker_id, "codex_unavailable", "Codex 进程已退出但任务未完成；从持久任务记录恢复并检查公司模型配置。", task_id=task_id)
        for upload in worker.get("uploads", []):
            elapsed = age(now, upload.get("queued_at"))
            if upload.get("attempts", 0) or elapsed is None or elapsed > upload_seconds:
                incident(worker_id, "result_upload_failed", "封存结果尚未上传 Gitee；检查中转网络、认证及本地 outbox，仅重试上传。", task_id=str(upload.get("task_id", "")))
        last = worker.get("last_result", {}) or {}
        if last.get("failure_code") in {"result_publish_failed", "publish_failed"}:
            incident(worker_id, "result_publish_failed", "结果发布失败；保留构建产物，仅重试 Gitee 发布。")
    for worker_id in expected - seen:
        incident(worker_id, "worker_offline", "没有收到预期服务器的快照；检查主机以及独立健康发布服务。")
        unknown_workers.add(worker_id)
    for task in document.get("tasks", []):
        if not isinstance(task, dict):
            raise ValueError("Task entries must be objects")
        state = task.get("state", task.get("status", task.get("stage")))
        if state == "queued":
            elapsed = age(now, task.get("created_at", task.get("updated_at")))
            if elapsed is None or elapsed > queue_seconds:
                incident(str(task.get("worker_id", "receiver")), "queue_overdue", "已投递任务长时间未返回结果；检查 worker、排队资源和任务进展。", task_id=str(task.get("task_id", "unknown")))

    active: dict[str, dict[str, Any]] = {}
    history = list(previous.get("history", []))
    pending = list(previous.get("pending_notifications", []))
    pending_ids = {item["id"] for item in pending}

    def notify(entry: dict[str, Any], transition: str) -> None:
        notification_id = hashlib.sha256(f"{entry['key']}:{entry['first_detected_at']}:{transition}".encode()).hexdigest()
        if notification_id not in pending_ids:
            pending.append({"id": notification_id, "transition": transition, "incident": entry, "created_at": iso(now)})
            pending_ids.add(notification_id)
        history.append({"at": iso(now), "key": entry["key"], "transition": transition})

    for key, entry in observed.items():
        prior = previous["active"].get(key)
        entry["first_detected_at"] = prior["first_detected_at"] if prior else iso(now)
        entry["last_seen_at"] = iso(now)
        active[key] = entry
        if not prior:
            notify(entry, "opened")
    for key, prior in previous["active"].items():
        if key in observed:
            continue
        if prior.get("worker_id") in unknown_workers or key in unknown_incidents:
            active[key] = prior
        else:
            notify({**prior, "resolved_at": iso(now)}, "recovered")
    return {"schema": SCHEMA, "updated_at": iso(now), "active": active,
            "pending_notifications": pending, "history": history[-1000:], "healthy": not active,
            "worker_health": worker_health}


def smtp_configuration(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    required = ("LOCAL_CI_SMTP_HOST", "LOCAL_CI_SMTP_FROM", "LOCAL_CI_SMTP_TO")
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise ValueError("Mail configuration is incomplete: " + ", ".join(missing))
    sender = env["LOCAL_CI_SMTP_FROM"].strip()
    recipients = [entry.strip() for entry in env["LOCAL_CI_SMTP_TO"].split(",") if entry.strip()]
    for address in [sender, *recipients]:
        if any(character in address for character in "\r\n") or "@" not in parseaddr(address)[1]:
            raise ValueError("Mail sender/recipient address is invalid")
    username = env.get("LOCAL_CI_SMTP_USERNAME", "")
    password = env.get("LOCAL_CI_SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("SMTP username and password must be configured together")
    use_ssl = env.get("LOCAL_CI_SMTP_SSL", "0").lower() in {"1", "true"}
    starttls = env.get("LOCAL_CI_SMTP_STARTTLS", "1").lower() in {"1", "true"}
    return {"host": env["LOCAL_CI_SMTP_HOST"], "port": int(env.get("LOCAL_CI_SMTP_PORT", "465" if use_ssl else "587")),
            "sender": sender, "recipients": recipients, "username": username, "password": password,
            "ssl": use_ssl, "starttls": starttls and not use_ssl}


def message_for(notification: dict[str, Any], sender: str, recipients: list[str]) -> EmailMessage:
    entry = notification["incident"]
    recovered = notification["transition"] == "recovered"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[Local CI {'恢复' if recovered else '异常'}] {entry['worker_id']} / {entry['code']}"
    message["Message-ID"] = f"<{notification['id']}@local-ci.invalid>"
    message.set_content("\n".join([f"状态：{'已恢复' if recovered else '需要处理'}", f"服务器：{entry['worker_id']}",
                                    f"问题：{entry['code']}", f"任务：{entry.get('task_id') or '无'}",
                                    *([f"环境代际：{entry['generation']}"] if entry.get("generation") else []), entry["detail"],
                                    f"首次发现：{entry['first_detected_at']}", f"本次通知：{notification['created_at']}"]))
    return message


def deliver(notification: dict[str, Any], *, outbox: Path | None = None, smtp: dict[str, Any] | None = None) -> None:
    if outbox is not None:
        outbox.mkdir(parents=True, exist_ok=True)
        message = message_for(notification, "simulation@local-ci.invalid", ["maintainer@local-ci.invalid"])
        # A deterministic filename makes outbox replay idempotent.
        output = outbox / (notification["id"] + ".eml")
        if not output.exists():
            output.write_bytes(message.as_bytes())
        return
    smtp = smtp or smtp_configuration()
    message = message_for(notification, smtp["sender"], smtp["recipients"])
    cls = smtplib.SMTP_SSL if smtp["ssl"] else smtplib.SMTP
    kwargs = {"context": ssl.create_default_context()} if smtp["ssl"] else {}
    with cls(smtp["host"], smtp["port"], timeout=30, **kwargs) as client:
        if smtp["starttls"]:
            client.starttls(context=ssl.create_default_context())
        if smtp["username"]:
            client.login(smtp["username"], smtp["password"])
        client.send_message(message)


def read_input(args: argparse.Namespace) -> dict[str, Any]:
    if args.url:
        parsed = urllib.parse.urlparse(args.url)
        if parsed.scheme != "https" or parsed.hostname != "gitee.com" or parsed.username or parsed.password:
            raise ValueError("Health URL must be an HTTPS Gitee URL without embedded credentials")
        headers = {"Accept": "application/json"}
        if os.environ.get("GITEE_HEALTH_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITEE_HEALTH_TOKEN"]
        try:
            with urllib.request.urlopen(urllib.request.Request(args.url, headers=headers), timeout=30) as response:
                document = json.load(response)
            if document.get("encoding") == "base64" and isinstance(document.get("content"), str):
                document = json.loads(base64.b64decode(document["content"]))
        except (OSError, ValueError) as exc:
            # Do not put credentials or remote response bodies in public incidents.
            document = {"workers": [], "source_error": type(exc).__name__}
    else:
        document = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Watchdog input must be a JSON object")
    if args.expected_worker:
        document["expected_workers"] = args.expected_worker
    if args.tasks_file:
        path = Path(args.tasks_file)
        tasks = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"source_error": True}
        if isinstance(tasks, dict):
            if tasks.get("source_error"):
                document["tasks_source_error"] = True
            tasks = tasks.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("tasks-file must contain a list or a tasks object")
        document["tasks"] = [*document.get("tasks", []), *tasks]
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--input", help="JSON file, or - for standard input")
    sources.add_argument("--url", help="Gitee raw/Contents API health snapshot URL")
    parser.add_argument("--state", required=True)
    parser.add_argument("--output")
    parser.add_argument("--mail-outbox")
    parser.add_argument("--tasks-file", help="Additional queued task records, combinable with --url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--expected-worker", action="append", default=[])
    parser.add_argument("--stale-seconds", type=int, default=1200)
    parser.add_argument("--upload-seconds", type=int, default=1200)
    parser.add_argument("--progress-seconds", type=int, default=1800)
    parser.add_argument("--queue-seconds", type=int, default=3600)
    parser.add_argument("--disk-free-bytes", type=int, default=5 * 1024**3)
    parser.add_argument("--now", help="Explicit UTC time for deterministic simulation")
    args = parser.parse_args()
    try:
        if min(args.stale_seconds, args.upload_seconds, args.progress_seconds, args.queue_seconds) <= 0 or args.disk_free_bytes < 0:
            raise ValueError("Watchdog thresholds must be positive")
        state_path = Path(args.state)
        now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
        if now is None:
            raise ValueError("--now must be an ISO timestamp with a timezone")
        document = read_input(args)
        if not args.dry_run:
            state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = contextlib.nullcontext() if args.dry_run else state_path.with_suffix(state_path.suffix + ".lock").open("a+")
        with lock as handle:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
            state = evaluate(document, previous, now=now, stale_seconds=args.stale_seconds,
                             upload_seconds=args.upload_seconds, progress_seconds=args.progress_seconds,
                             queue_seconds=args.queue_seconds, disk_free_bytes=args.disk_free_bytes)
            if not args.dry_run:
                # Validate SMTP even for a healthy run: an inert monitor is a deployment error.
                atomic_json(state_path, state)
                try:
                    smtp = None if args.mail_outbox else smtp_configuration()
                    for notification in list(state["pending_notifications"]):
                        deliver(notification, outbox=Path(args.mail_outbox) if args.mail_outbox else None, smtp=smtp)
                        state["pending_notifications"] = [item for item in state["pending_notifications"] if item["id"] != notification["id"]]
                        atomic_json(state_path, state)
                finally:
                    if args.output:
                        atomic_json(Path(args.output), state)
            print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, smtplib.SMTPException) as exc:
        print(f"Local CI watchdog failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
