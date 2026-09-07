#!/usr/bin/env python3
"""Auditable local migration checklist; never executes service or remote actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environments.manager import SHA_RE, atomic_json

SCHEMA = "triton-anchor-local-ci-migration/v1"
PHASES = ("compatibility_ready", "main_ready", "old_intake_stopped", "old_tasks_drained", "poller_ready", "verified")


def plan(worker_revision: str, main_revision: str) -> dict:
    if not SHA_RE.fullmatch(worker_revision) or not SHA_RE.fullmatch(main_revision):
        raise ValueError("Migration requires exact worker and main commit identities")
    return {"schema": SCHEMA, "worker_revision_sha": worker_revision, "main_revision_sha": main_revision,
            "phases": list(PHASES), "completed": [], "events": [], "next_phase": PHASES[0], "production_actions_executed": False}


def drained(tasks: list[dict]) -> bool:
    for task in tasks:
        if not isinstance(task, dict) or not task.get("task_id"):
            raise ValueError("Old task inventory requires task_id")
        state = task.get("state")
        if state not in {"complete", "cancelled", "failed"}:
            raise ValueError("Old task inventory still contains nonterminal work")
        if state in {"complete", "failed"} and not task.get("receiver_confirmed"):
            raise ValueError("Old completed work still needs receiver/publication confirmation")
        if state == "cancelled" and not task.get("reason"):
            raise ValueError("Cancelled old work needs an explicit recorded reason")
        evidence = Path(task.get("evidence_path", ""))
        if not evidence.is_file():
            raise ValueError("Old task terminal state requires a local saved evidence file")
    return True


def advance(state: dict, phase: str, evidence_path: Path) -> dict:
    if state.get("schema") != SCHEMA or state.get("next_phase") != phase:
        raise ValueError("Migration phases must be recorded in order")
    payload = evidence_path.read_bytes()
    evidence = json.loads(payload)
    if not isinstance(evidence, dict):
        raise ValueError("Migration evidence must be a JSON object")
    required = {
        "compatibility_ready": ("receiver_accepts_v4", "legacy_results_display_only", "worker_preflight_ready"),
        "main_ready": ("minimal_main_dispatch_verified",),
        "old_intake_stopped": ("old_intake_stopped",),
        "old_tasks_drained": (),
        "poller_ready": ("old_poller_stopped", "new_poller_ready", "independent_health_ready", "external_watchdog_ready"),
        "verified": ("simulated_or_live_roundtrip_verified", "cancel_verified", "publish_retry_verified", "receipt_verified", "rollback_available"),
    }[phase]
    if any(evidence.get(key) is not True for key in required):
        raise ValueError("Required migration evidence has not been confirmed")
    if phase in {"compatibility_ready", "poller_ready"} and evidence.get("worker_revision_sha") != state["worker_revision_sha"]:
        raise ValueError("Worker evidence does not match the frozen migration revision")
    if phase == "main_ready" and evidence.get("main_revision_sha") != state["main_revision_sha"]:
        raise ValueError("Main evidence does not match the migration revision")
    if phase == "old_tasks_drained":
        tasks = evidence.get("tasks")
        if not isinstance(tasks, list) or evidence.get("inventory_complete") is not True:
            raise ValueError("Complete old-task inventory must be explicitly recorded")
        drained(tasks)
    result = {**state, "completed": [*state["completed"], phase], "events": [*state["events"], {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "phase": phase,
        "evidence_path": str(evidence_path.resolve()), "evidence_sha256": hashlib.sha256(payload).hexdigest()}]}
    count = len(result["completed"])
    result["next_phase"] = PHASES[count] if count < len(PHASES) else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "record", "status"))
    parser.add_argument("--state", help="Optional for plan; record/status require a local state artifact")
    parser.add_argument("--worker-revision")
    parser.add_argument("--main-revision")
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--evidence")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = plan(args.worker_revision or "", args.main_revision or "")
            if args.state:
                destination = Path(args.state)
                if destination.exists():
                    raise ValueError("Migration state already exists; use status/record instead of replacing it")
                atomic_json(destination, result)
        else:
            if not args.state:
                parser.error("--state is required")
            result = json.loads(Path(args.state).read_text())
            if args.command == "record":
                if not args.phase or not args.evidence:
                    parser.error("--phase and --evidence are required")
                result = advance(result, args.phase, Path(args.evidence))
                atomic_json(Path(args.state), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Local CI migration record failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
