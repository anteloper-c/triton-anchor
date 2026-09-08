"""Run on an independent host/CI to detect lost Local CI heartbeat publication."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .health import issue, timestamp
from .notify import Notifier, recipients, validate_authentication
from .workers import atomic_json


def evaluate(snapshot, worker_id, max_age=300, now=None):
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict):
        return [issue("heartbeat_invalid", "心跳快照不是对象。")]
    if snapshot.get("worker_id") != worker_id or snapshot.get("schema") != "triton-anchor-local-ci-worker-health":
        return [issue("heartbeat_invalid", "心跳来源或 Schema 不匹配。")]
    seen = timestamp(snapshot.get("heartbeat_at"))
    if seen is None or now - seen > max_age or seen > now + 60:
        return [issue("host_offline", "主机心跳过期或时间异常；请检查主机、电源、网络及健康发布服务。")]
    issues = snapshot.get("issues", [])
    if not isinstance(issues, list) or len(issues) > 100 or any(
        not isinstance(i, dict) or not isinstance(i.get("code"), str) or not isinstance(i.get("message"), str)
        for i in issues
    ):
        return [issue("heartbeat_invalid", "心跳异常列表格式无效。")]
    if snapshot.get("state") not in {"healthy", "degraded"} or (snapshot.get("state") == "degraded" and not issues):
        return [issue("heartbeat_invalid", "心跳健康状态不完整。")]
    return issues


def notify(config, issues, dry_run=False):
    """An absent mail transport never disables detection or records a delivery."""
    smtp = config.get("smtp", {})
    supplied = any(os.environ.get(name) for name in (
        "LOCAL_CI_SMTP_USERNAME", "LOCAL_CI_SMTP_PASSWORD", "LOCAL_CI_SMTP_REFRESH_TOKEN"))
    if isinstance(smtp, dict) and not smtp and not supplied:
        if not any(item["code"] == "notification_not_configured" for item in issues):
            issues.append(issue("notification_not_configured", "邮件未配置；外部心跳检查仍执行。"))
        return {"status": "not_configured"}
    try:
        if not isinstance(smtp, dict) or not smtp.get("host") or not smtp.get("from"):
            raise ValueError("SMTP host/from not configured")
        if not isinstance(smtp["host"], str) or "\r" in smtp["host"] or "\n" in smtp["host"]:
            raise ValueError("invalid SMTP host")
        sender = smtp["from"]
        if not isinstance(sender, str) or "@" not in sender or "\r" in sender or "\n" in sender:
            raise ValueError("invalid SMTP sender")
        recipients(smtp)
        validate_authentication(smtp)
        if smtp.get("oauth2") is None:
            username = os.environ.get(smtp.get("username_env", "LOCAL_CI_SMTP_USERNAME"), "")
            password = os.environ.get(smtp.get("password_env", "LOCAL_CI_SMTP_PASSWORD"), "")
            if bool(username) != bool(password):
                raise ValueError("SMTP username/password configuration is incomplete")
        if not 1 <= int(smtp.get("port", 465 if smtp.get("ssl") else 587)) <= 65535:
            raise ValueError("invalid SMTP port")
    except (ValueError, TypeError, AttributeError):
        # Keep configuration faults visible even before the first incident. Do
        # not echo config values or mark a notification as delivered.
        issues.append(issue("notification_configuration_error", "邮件配置不完整或无效；请检查发件服务、收件人及授权。"))
        return {"status": "pending", "error_type": "ValueError"}
    return Notifier(config["state_dir"], smtp).update(config["worker_id"], issues, dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="trusted watchdog JSON")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    url = config["heartbeat_url"]
    if not url.startswith("https://"):
        raise ValueError("independent heartbeat URL must use HTTPS")
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    token = os.environ.get(config.get("token_env", "LOCAL_CI_HEARTBEAT_READ_TOKEN"), "")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("heartbeat snapshot too large")
        snapshot = json.loads(data)
        issues = evaluate(snapshot, config["worker_id"], config.get("max_age_seconds", 300))
    except (OSError, URLError, ValueError, TypeError, AttributeError):
        issues = [issue("heartbeat_unreachable", "外部监控无法读取健康快照；请检查主机与健康发布通道。")]
    # Copy the snapshot's list before appending independent monitor diagnostics.
    issues = list(issues)
    result = {"checked_at": time.time(), "worker_id": config["worker_id"], "issues": issues}
    result["notification"] = notify(config, issues, args.dry_run)
    missing_mail = result["notification"]["status"] == "not_configured"
    operational_issues = [item for item in issues if not (missing_mail and item["code"] == "notification_not_configured")]
    atomic_json(Path(config["state_dir"]) / "watchdog-latest.json", result)
    print(json.dumps({"issues": [i["code"] for i in issues], "notification": result["notification"]["status"]}))
    return 1 if operational_issues or result["notification"]["status"] == "pending" else 0


if __name__ == "__main__":
    raise SystemExit(main())
