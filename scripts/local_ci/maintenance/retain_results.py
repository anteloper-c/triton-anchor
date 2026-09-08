#!/usr/bin/env python3
"""Expire Gitee v4 run trees by upload age, independently of GitHub publication.

Default is a dry run. Only --apply commits deletions to the configured results
branch; small expiry records remain for history and to prevent older-run replay.
Git history is retained: this is logical retention, not repository compaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_ci.protocol import RESULT_SCHEMA, ContractError, canonical
from agent_ci.relay import GitRelay

RESULT_PATH = re.compile(r"runs/v4/([0-9a-f]{64})/([A-Za-z0-9][A-Za-z0-9_.-]{0,119})/result\.json\Z")


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def retain_results(relay: GitRelay, days: int = 30, *, now: float | None = None, apply: bool = False) -> dict:
    if type(days) is not int or days <= 0:
        raise ValueError("results_retention_days must be a positive integer")
    now = time.time() if now is None else now
    branch = relay.results_branch
    if branch != "local-ci-results":
        raise ContractError("Retention is restricted to the local-ci-results branch")
    with relay.lock:
        for attempt in range(3):
            try:
                with tempfile.TemporaryDirectory(prefix="retention-", dir=relay.root) as directory:
                    work = Path(directory)
                    relay.git(["init", "-q"], cwd=work)
                    relay.git(["remote", "add", "origin", relay.url], cwd=work)
                    present = relay.git(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=work).stdout.strip()
                    report = {"applied": apply, "retention_days": days, "expired": [], "skipped": []}
                    if not present:
                        return report
                    relay.git(["fetch", "--quiet", "origin", f"refs/heads/{branch}"], cwd=work)
                    relay.git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=work)
                    paths = relay.git(["ls-tree", "-r", "--name-only", "HEAD", "--", "runs/v4/"], cwd=work).stdout.decode().splitlines()
                    for relative in paths:
                        match = RESULT_PATH.fullmatch(relative)
                        if not match:
                            continue
                        task_id, run_id = match.groups()
                        source = work / relative
                        # Never follow candidate-created links when inspecting evidence.
                        if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != work and p.is_relative_to(work)):
                            report["skipped"].append({"path": relative, "reason": "symlink"})
                            continue
                        uploaded = int(relay.git(["log", "-1", "--format=%ct", "HEAD", "--", relative], cwd=work).stdout.strip())
                        if uploaded <= 0 or now - uploaded < days * 86400:
                            continue
                        raw = source.read_bytes()
                        try:
                            result = json.loads(raw)
                            valid = (isinstance(result, dict) and result.get("schema") == RESULT_SCHEMA
                                     and isinstance(result.get("task"), dict)
                                     and result["task"].get("task_id") == task_id and result.get("run_id") == run_id)
                        except (ValueError, UnicodeError):
                            valid = False
                        if not valid:
                            report["skipped"].append({"path": relative, "reason": "invalid_result_identity"})
                            continue
                        marker = {"schema": "triton-anchor-result-retention/v1", "task_id": task_id, "run_id": run_id,
                                  "result_digest": hashlib.sha256(raw).hexdigest(), "uploaded_at": iso(uploaded),
                                  "expired_at": iso(now), "reason": "retention_expired"}
                        target = work / "retention/v4" / task_id / (run_id + ".json")
                        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != work and p.is_relative_to(work)):
                            raise ContractError("Retention marker path contains a symlink")
                        if target.exists():
                            raise ContractError("Result and retention marker coexist; inspect results history")
                        report["expired"].append(marker)
                        if apply:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(canonical(marker) + b"\n")
                            relay.git(["rm", "-r", "--quiet", "--", source.parent.relative_to(work).as_posix()], cwd=work)
                            relay.git(["add", "--", target.relative_to(work).as_posix()], cwd=work)
                    if apply and report["expired"]:
                        relay.git(["-c", "user.name=local-ci", "-c", "user.email=local-ci@example.invalid",
                                   "commit", "--quiet", "-m", "ci: expire result evidence by upload age"], cwd=work)
                        # Normal fast-forward push; concurrent uploads force a fresh retry.
                        relay.git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=work)
                    return report
            except RuntimeError:
                if attempt == 2:
                    raise
    raise RuntimeError("Result retention did not finish")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        relay = GitRelay(config["gitee_repo_url"], Path(config["state_dir"]) / "result-retention")
        report = retain_results(relay, config.get("results_retention_days", 30), apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["skipped"] else 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"Result retention failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
