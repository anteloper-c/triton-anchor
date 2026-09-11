"""Task lifecycle: execute once, stop before collecting, retry publication only."""

import hashlib
import json
from pathlib import Path

import pytest

from agent_ci.protocol import TASK_SCHEMA, atomic_json, metadata_digest, task_id
from agent_ci.worker import Worker


def manifest():
    value = {
        "schema": TASK_SCHEMA,
        "repository": "likehupochuan/triton-anchor",
        "event_kind": "pull_request",
        "pr_number": 7,
        "target_branch": "main",
        "tested_sha": "a" * 40,
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "worker_revision_sha": "d" * 40,
        "llvm_hash": "e" * 40,
        "full": False,
        "draft": False,
        "state": "open",
        "title": "Fix behavior",
        "description": "Implementation and verification",
        "labels": [],
        "captured_at": "2026-09-11T00:00:00Z",
    }
    value["metadata_digest"] = metadata_digest(value)
    value["task_id"] = task_id(value)
    prefix = f"ci/pr-7/{value['task_id']}"
    value.update(
        task_ref=prefix + "/tested",
        base_task_ref=prefix + "/base",
        head_task_ref=prefix + "/head",
    )
    return value


@pytest.mark.parametrize("cancelled", [False, True])
def test_run_stops_collects_and_publishes_without_reexecuting_on_network_failure(
    tmp_path, monkeypatch, cancelled
):
    task = manifest()
    events = []
    calls = 0
    uploads = 0

    class Relay:
        def refresh(self):
            pass

        def validity(self, task):
            return True, ""

        def tasks(self):
            return [task]

        def publish_result(self, task, run_id, directory):
            nonlocal uploads
            uploads += 1
            if uploads == 1:
                raise OSError("temporary relay outage")
            return hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def acquire_task(self, task, run_id):
            return {
                "run_id": run_id,
                "profile": "fixture",
                "backend_enabled": False,
                "environment_fingerprint": "fixture",
                "llvm_hash": task["llvm_hash"],
                "image_id": "fixture",
            }

        def stop_task(self, handle):
            events.append("stop")

        def collect_artifacts(self, handle):
            assert events[-1] == "stop"
            events.append("collect")

        def destroy_task(self, handle):
            events.append("destroy")

    class Executor:
        def __init__(self, config, state_dir, generation, task, relay, *, manager):
            self.run_dir = (
                Path(state_dir) / "runs" / task["task_id"] / generation["run_id"]
            )

        def prepare(self, variant="candidate"):
            return tmp_path

        def write_context(self, policy, changes):
            pass

    class Driver:
        def redact(self, text):
            return text

        def run(self, executor, **kwargs):
            nonlocal calls
            calls += 1
            atomic_json(
                executor.run_dir / "artifacts/agent-result.json",
                {
                    "status": "pass",
                    "summary": "Behavior verified",
                    "checks": [],
                    "reviews": [
                        {"kind": kind, "status": "pass", "summary": "Verified"}
                        for kind in ("pr_info", "architecture")
                    ],
                },
            )
            if cancelled:
                worker.active.cancel("PR closed")
            return {"exit_code": 0, "reason": ""}

    monkeypatch.setattr(
        "agent_ci.worker.changed_files", lambda *args: [{"path": "README.md"}]
    )
    monkeypatch.setattr(
        "agent_ci.worker.minimum_checks",
        lambda *args, **kwargs: {"required_checks": []},
    )
    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True, "retry_delay_seconds": 0},
        relay=Relay(),
        manager=Manager(),
        driver=Driver(),
        executor_factory=Executor,
    )
    worker.scan()
    row = worker.journal.task(task["task_id"])
    assert row["phase"] == "publish_pending"
    result = json.loads(
        (worker.journal.run_dir(task["task_id"]) / "sealed/result.json").read_text()
    )
    assert result["status"] == ("cancelled" if cancelled else "pass")
    assert events[-3:] == ["stop", "collect", "destroy"]
    worker.scan()
    assert calls == 1 and uploads == 2
    assert worker.journal.task(task["task_id"])["phase"] == "published"
    assert worker.journal.task(task["task_id"])["run_id"] == row["run_id"]
