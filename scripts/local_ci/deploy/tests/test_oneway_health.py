from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL))
from agent_ci.state import Journal
from agent_ci.protocol import canonical
from deploy.health import collect


def test_health_reports_upload_backlog_without_waiting_for_github():
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary)
        journal = Journal(state)
        task = {"task_id": "a" * 64, "repository": "likehupochuan/triton-anchor", "event_kind": "push", "target_branch": "main", "pr_number": None}
        journal.register(task)
        payload = state / "result.json"
        payload.write_bytes(canonical({"status": "pass"}))
        journal.queue_result(task["task_id"], payload, hashlib.sha256(payload.read_bytes()).hexdigest())
        (state / "health").mkdir()
        (state / "health/worker.json").write_text(json.dumps({"heartbeat_at": 1000, "pid": os.getpid(), "codex_alive": False}))
        class Manager:
            def health(self):
                return {"active": {}, "generations": []}
        config = {"state_dir": str(state), "profiles": {}, "monitor_services": []}
        before = collect(config, now=1000, manager=Manager())
        assert before["uploads"][0]["task_id"] == task["task_id"]
        assert "codex_alive" not in before["active_task"]
        journal.published(task["task_id"])
        after = collect(config, now=1000, manager=Manager())
        assert after["uploads"] == []
        assert after["active_task"] is None
        assert "receipts" not in after


def test_resumed_run_upload_age_does_not_inherit_previous_run_wait():
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary)
        journal = Journal(state)
        task = {"task_id": "b" * 64, "repository": "likehupochuan/triton-anchor", "event_kind": "push", "target_branch": "main", "pr_number": None}
        journal.register(task)
        payload = state / "old-result.json"
        payload.write_bytes(canonical({"status": "infra_error"}))
        journal.queue_result(task["task_id"], payload, hashlib.sha256(payload.read_bytes()).hexdigest())
        journal.published(task["task_id"])
        with journal.connect() as db:
            db.execute("UPDATE events SET at=1 WHERE kind='phase:publishing'")
        journal.resume(task["task_id"])
        new_payload = state / "new-result.json"
        new_payload.write_bytes(canonical({"status": "pass"}))
        journal.queue_result(task["task_id"], new_payload, hashlib.sha256(new_payload.read_bytes()).hexdigest())
        class Manager:
            def health(self):
                return {"active": {}, "generations": []}
        result = collect({"state_dir": str(state), "profiles": {}, "monitor_services": []}, manager=Manager())
        assert not result["uploads"][0]["queued_at"].startswith("1970-")
