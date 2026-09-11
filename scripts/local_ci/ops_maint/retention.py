#!/usr/bin/env python3
"""Expire published evidence after 30 days; preserve pending delivery and summaries."""

from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ops_maint.artifacts import atomic_json


def _timestamp(value):
    if type(value) in (int, float):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def _size(root):
    return sum(
        p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )


def _remove_tree(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            "Evidence retention target must stay within the configured run"
        )
    if path.exists():
        shutil.rmtree(path)


def expire_delivery(config, run, index, *, client=None, relay=None):
    from agent_ci.delivery import delivery_lock

    with delivery_lock(run):
        current = run / "delivery-index.json"
        if current.exists():
            index = json.loads(current.read_text())
        return _expire_delivery(config, run, index, client=client, relay=relay)


def _expire_delivery(config, run, index, *, client=None, relay=None):
    """Publish expiry before deleting attachments; resolve uncertain deletes."""
    from agent_ci.delivery import GiteeReleaseClient
    from agent_ci.relay import GitRelay
    from agent_ci.protocol import canonical

    rows = index.get("artifacts", [])
    if any(
        a.get("required") and a.get("status") not in {"ready", "expired"} for a in rows
    ):
        raise ValueError("Required pending evidence cannot expire")
    for artifact in rows:
        if artifact.get("status") in {"ready", "pending"}:
            artifact["status"] = "expired"
            artifact["reason"] = "retention_expired"
    index["status"] = "expired"
    atomic_json(run / "delivery-index.json", index)
    relay = relay or GitRelay(
        config["gitee_repo_url"], Path(config["state_dir"]) / "retention-relay"
    )
    prefix = "runs/v4/" + run.parent.name + "/" + run.name
    relay.write(
        relay.results_branch,
        {prefix + "/delivery-index.json": canonical(index) + b"\n"},
    )
    client = client or GiteeReleaseClient(config["gitee_repo_url"])
    present = {}
    for artifact in rows:
        release, attachment = artifact.get("release_id"), artifact.get("attachment_id")
        if not release or not attachment or artifact.get("deleted"):
            continue
        if release not in present:
            present[release] = {a["id"] for a in client.attachments(release)}
        if attachment in present[release]:
            client.delete(release, attachment)
            present[release].discard(attachment)
        artifact["deleted"] = True
        atomic_json(run / "delivery-index.json", index)
    relay.write(
        relay.results_branch,
        {prefix + "/delivery-index.json": canonical(index) + b"\n"},
    )
    return index


def retain_local(config, *, now=None, apply=True, client=None, relay=None):
    now = time.time() if now is None else now
    days = config.get("results_retention_days", 30)
    budget = config.get(
        "evidence_max_bytes", config.get("task_workspace_max_bytes", 100 * 1024**3)
    )
    if type(days) is not int or days <= 0 or type(budget) is not int or budget <= 0:
        raise ValueError(
            "Positive retention days and evidence byte budget are required"
        )
    state = Path(config["state_dir"]).resolve()
    runs = state / "runs"
    report = {
        "retention_days": days,
        "max_bytes": budget,
        "expired": [],
        "protected": [],
        "errors": [],
        "applied": apply,
    }
    for path in sorted(runs.glob("*/*/state.json")):
        run = path.parent
        if any(p.is_symlink() for p in (run, path, run.parent)):
            report["errors"].append({"run": run.name, "reason": "symlink"})
            continue
        try:
            record = json.loads(path.read_text())
            delivery = record.get("delivery") or {}
            published = _timestamp(
                delivery.get("published", record.get("published_at"))
            )
            index_path = run / "delivery-index.json"
            index = json.loads(index_path.read_text()) if index_path.is_file() else None
            required_pending = (
                delivery.get("required_pending") or delivery.get("ready") is False
            )
            if index:
                required_pending = required_pending or any(
                    r.get("required") and r.get("status") not in {"ready", "expired"}
                    for r in index.get("artifacts", [])
                )
            hold_until = _timestamp(record.get("retention_until"))
            if (
                record.get("phase") != "published"
                or required_pending
                or not published
                or hold_until > now
            ):
                report["protected"].append(
                    {"task_id": run.parent.name, "run_id": run.name}
                )
                continue
            if now - published < days * 86400 or (run / "retention.json").exists():
                continue
            marker = {
                "schema": "triton-anchor-result-retention/v1",
                "task_id": run.parent.name,
                "run_id": run.name,
                "expired_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "reason": "retention_expired",
                "published_at": published,
            }
            if apply:
                if index:
                    expire_delivery(config, run, index, client=client, relay=relay)
                for name in ("artifacts", "logs"):
                    _remove_tree(run / name, run)
                # Sealed result and command metadata remain queryable. Large
                # payload subtrees may be removed once published and expired.
                for name in ("artifacts", "logs", "evidence"):
                    _remove_tree(run / "sealed" / name, run)
                atomic_json(run / "retention.json", marker)
            report["expired"].append(marker)
        except (OSError, ValueError, RuntimeError) as exc:
            report["errors"].append(
                {
                    "task_id": run.parent.name,
                    "run_id": run.name,
                    "reason": type(exc).__name__,
                }
            )
    used = _size(runs) if runs.exists() else 0
    free = shutil.disk_usage(state).free if state.exists() else 0
    report.update(
        logical_bytes=used,
        state_free_bytes=free,
        pause_intake=used > budget
        or free < config.get("state_min_free_bytes", 5 * 1024**3),
    )
    if apply:
        atomic_json(state / "health/retention.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    report = retain_local(config, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
