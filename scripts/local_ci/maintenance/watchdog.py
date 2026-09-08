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
from .notify import Notifier
from .workers import atomic_json


def evaluate(snapshot, worker_id, max_age=300, now=None):
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict):
        return [issue("heartbeat_invalid", "心跳快照不是对象。")]
    if snapshot.get("worker_id") != worker_id or snapshot.get("schema") != "triton-anchor-local-ci-worker-health/v2":
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
    result = {"checked_at": time.time(), "worker_id": config["worker_id"], "issues": issues}
    result["notification"] = Notifier(config["state_dir"], config.get("smtp", {})).update(config["worker_id"], issues, args.dry_run)
    atomic_json(Path(config["state_dir"]) / "watchdog-latest.json", result)
    print(json.dumps({"issues": [i["code"] for i in issues], "notification": result["notification"]["status"]}))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
