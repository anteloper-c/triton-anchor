"""Independent local health collection; persistent snapshots power Dashboard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

from .notify import Notifier, recipients
from .workers import WorkerError, WorkerManager, atomic_json, read_json


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def issue(code, message, **extra):
    return {"code": code, "message": message, **extra}


def read_status(path, issues, code):
    try:
        value = read_json(path, {})
        if not isinstance(value, dict):
            raise ValueError("status must be an object")
        return value
    except (OSError, ValueError):
        issues.append(issue(code, "服务状态文件无法解析，请检查磁盘与写入进程。"))
        return {}


def collect(config, manager=None, now=None, service_runner=subprocess.run):
    """Read actual Docker/disk/service/heartbeat state. Never repair or send mail."""
    now = time.time() if now is None else now
    root = Path(config["state_dir"])
    health = config.get("health", {})
    manager = manager or WorkerManager(root, docker=config.get("docker", "docker"))
    issues, workers = [], []
    poller = read_status(root / "health" / "poller.json", issues, "poller_status_invalid")
    last_seen = timestamp(poller.get("heartbeat_at"))
    if last_seen is None or now - last_seen > health.get("poller_max_age_seconds", 180):
        issues.append(issue("poller_stale", "Poller 心跳缺失或过期，请检查服务与任务进程。"))
    elif poller.get("state") in {"error", "failed", "publish_pending"}:
        issues.append(issue("poller_" + poller["state"], "Poller 报告异常，请检查本地日志和待发布结果。"))
    task = read_status(root / "health" / "task.json", issues, "task_status_invalid")
    if task.get("state") not in {None, "idle", "completed", "cancelled", "failed"}:
        started = timestamp(task.get("started_at"))
        heartbeat = timestamp(task.get("heartbeat_at"))
        if heartbeat is None or now - heartbeat > health.get("task_heartbeat_max_age_seconds", 300):
            issues.append(issue("task_stalled", "任务执行心跳过期；需确认 Codex 与被测进程状态。"))
        if started is not None and now - started > health.get("task_max_age_seconds", 14400):
            issues.append(issue("task_overdue", "任务超过最长预期时长，请检查构建、Codex 或发布阶段。"))
    if task.get("phase") == "publish_pending":
        issues.append(issue("publication_pending", "结果已经保留；发布待重试，不应重新执行构建测试。"))
    publication = read_status(root / "health-publication" / "latest.json", issues, "health_publication_status_invalid")
    if publication.get("status") == "pending":
        issues.append(issue("health_publication_pending", "健康快照发布失败，已保留本地快照等待重试。"))
    disks = []
    for target in health.get("disk_paths", [str(root)]):
        try:
            usage = shutil.disk_usage(target)
            disks.append({"path": str(target), "total_bytes": usage.total, "free_bytes": usage.free})
            if usage.free < float(health.get("min_free_gb", 20)) * 1024**3:
                issues.append(issue("disk_low", "可用磁盘空间不足，请按保留策略检查任务产物与环境。", path=str(target)))
        except OSError:
            issues.append(issue("disk_unavailable", "配置的磁盘路径不可读。", path=str(target)))
    for profile in config.get("profiles", []):
        try:
            state = manager.inspect(profile)
            workers.append(state)
            if not state["running"] and not state.get("draining"):
                issues.append(issue("container_unavailable", "固定版本容器未运行。", profile_id=profile["id"]))
            if state.get("oom_killed"):
                issues.append(issue("container_oom", "容器记录 OOM；检查内存与编译并行度。", profile_id=profile["id"]))
            if state.get("last_error"):
                issues.append(issue("maintenance_failed", "环境重建失败，检查维护日志及回滚状态。", profile_id=profile["id"]))
        except (WorkerError, OSError, ValueError, subprocess.SubprocessError):
            issues.append(issue("docker_unavailable", "无法读取 Docker worker 状态。", profile_id=profile["id"]))
    for service in health.get("systemd_services", []):
        try:
            result = service_runner(["systemctl", "is-active", "--quiet", service], timeout=15, capture_output=True)
            if result.returncode:
                issues.append(issue("service_unavailable", f"systemd 服务未运行：{service}", service=service))
        except (OSError, subprocess.SubprocessError):
            issues.append(issue("service_unavailable", f"无法读取 systemd 服务：{service}", service=service))
    smtp = config.get("smtp", {})
    try:
        recipients(smtp)
        if not smtp.get("host") or not smtp.get("from"):
            raise ValueError("SMTP host/from not configured")
        notification_config = "configured"
    except ValueError as exc:
        notification_config = str(exc)
        issues.append(issue("notification_not_configured", notification_config))
    return {"schema": "triton-anchor-local-ci-worker-health/v2", "worker_id": config["worker_id"],
            "heartbeat_at": now, "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "state": "degraded" if issues else "healthy", "issues": issues, "workers": workers,
            "poller": poller, "task": task, "disks": disks, "notification_config": notification_config}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="collect health but never send mail")
    parser.add_argument("--publish", action="store_true", help="publish health through the independent trusted Git checkout")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    result = collect(config)
    root = Path(config["state_dir"]) / "health"
    result["notification"] = Notifier(root, config.get("smtp", {})).update(config["worker_id"], result["issues"], args.dry_run)
    atomic_json(root / "latest.json", result)
    if not args.dry_run and (args.publish or config.get("health", {}).get("publish", False)):
        from .publish_health import HealthPublisher, PublicationError
        try:
            result["publication"] = HealthPublisher(config).publish()
        except (PublicationError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result["publication"] = {"status": "pending", "error_type": type(exc).__name__, "updated_at": time.time()}
            atomic_json(Path(config["state_dir"]) / "health-publication/latest.json", result["publication"])
    print(json.dumps({"state": result["state"], "issues": [i["code"] for i in result["issues"]],
                      "notification": result["notification"]["status"],
                      "publication": result.get("publication", {}).get("status", "not_requested")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
