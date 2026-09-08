from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "watchdog.py"
spec = importlib.util.spec_from_file_location("local_ci_watchdog", MODULE)
watchdog = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.worker = {"worker_id": "worker-1", "collected_at": watchdog.iso(self.now), "state": "healthy", "poller": {"alive": True}, "container": {"running": True}}

    def test_healthy_has_no_incidents(self):
        self.assertTrue(watchdog.evaluate(self.worker, now=self.now)["healthy"])

    def test_external_offline_detection_and_deduplication(self):
        worker = {**self.worker, "collected_at": watchdog.iso(self.now - timedelta(hours=1))}
        first = watchdog.evaluate(worker, now=self.now)
        self.assertEqual(len(first["pending_notifications"]), 1)
        first["pending_notifications"] = []
        second = watchdog.evaluate(worker, first, now=self.now + timedelta(minutes=1))
        self.assertFalse(second["pending_notifications"])
        self.assertEqual(len(second["active"]), 1)

    def test_recovery_notification_once(self):
        prior = watchdog.evaluate({**self.worker, "state": "offline"}, now=self.now)
        prior["pending_notifications"] = []
        recovered = watchdog.evaluate(self.worker, prior, now=self.now)
        self.assertTrue(recovered["healthy"])
        self.assertEqual(recovered["pending_notifications"][0]["transition"], "recovered")

    def test_missing_source_does_not_claim_other_incidents_recovered(self):
        prior = watchdog.evaluate({**self.worker, "container": {"running": False}}, now=self.now)
        prior["pending_notifications"] = []
        unavailable = watchdog.evaluate({"source_error": "network", "workers": []}, prior, now=self.now)
        self.assertIn("worker-1:container_unavailable", unavailable["active"])
        self.assertFalse(any(item["transition"] == "recovered" for item in unavailable["pending_notifications"]))

    def test_expected_worker_absent_is_offline(self):
        result = watchdog.evaluate({"workers": [], "expected_workers": ["missing"]}, now=self.now)
        self.assertIn("missing:worker_offline", result["active"])

    def test_failed_upload_alerts_then_recovers_without_codex_or_github(self):
        worker = {**self.worker, "active_task": {"task_id": "task-1", "stage": "publishing", "codex_alive": False},
                  "uploads": [{"task_id": "task-1", "attempts": 1, "queued_at": watchdog.iso(self.now)}]}
        result = watchdog.evaluate(worker, now=self.now)
        self.assertEqual({"worker-1:result_upload_failed:task-1"}, set(result["active"]))
        result["pending_notifications"] = []
        self.assertFalse(watchdog.evaluate(worker, result, now=self.now)["pending_notifications"])
        recovered = watchdog.evaluate(self.worker, result, now=self.now)
        self.assertTrue(recovered["healthy"])
        self.assertEqual("recovered", recovered["pending_notifications"][0]["transition"])

    def test_uploaded_result_does_not_wait_for_github(self):
        worker = {**self.worker, "tasks": [], "uploads": []}
        self.assertTrue(watchdog.evaluate(worker, now=self.now)["healthy"])

    def test_disk_codex_and_stuck_task(self):
        worker = {**self.worker, "storage": [{"free_bytes": 12}], "active_task": {"task_id": "task", "codex_alive": False,
                  "last_progress_at": watchdog.iso(self.now - timedelta(hours=1))}}
        result = watchdog.evaluate(worker, now=self.now)
        self.assertEqual({entry["code"] for entry in result["active"].values()}, {"disk_space_low", "codex_unavailable", "task_no_progress"})

    def test_mail_config_missing_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "configuration is incomplete"):
            watchdog.smtp_configuration({})

    def test_outbox_replay_is_idempotent(self):
        result = watchdog.evaluate({**self.worker, "state": "offline"}, now=self.now)
        notification = result["pending_notifications"][0]
        with tempfile.TemporaryDirectory() as temporary:
            outbox = Path(temporary)
            watchdog.deliver(notification, outbox=outbox)
            watchdog.deliver(notification, outbox=outbox)
            self.assertEqual(len(list(outbox.glob("*.eml"))), 1)

    def test_cli_stdin_saves_incidents_and_simulated_mail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, str(MODULE), "--input", "-", "--state", str(root / "state.json"),
                       "--mail-outbox", str(root / "outbox"), "--now", watchdog.iso(self.now)]
            data = json.dumps({"workers": [], "expected_workers": ["worker"]})
            first = subprocess.run(command, input=data, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run(command, input=data, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(list((root / "outbox").glob("*.eml"))), 1)
            self.assertFalse(json.loads((root / "state.json").read_text())["pending_notifications"])

    def test_missing_smtp_preserves_pending_and_dashboard_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {key: value for key, value in os.environ.items() if not key.startswith("LOCAL_CI_SMTP_")}
            command = [sys.executable, str(MODULE), "--input", "-", "--state", str(root / "state.json"),
                       "--output", str(root / "dashboard.json"), "--now", watchdog.iso(self.now)]
            result = subprocess.run(command, input=json.dumps({"workers": [], "expected_workers": ["worker"]}), env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(json.loads((root / "state.json").read_text())["pending_notifications"])
            self.assertTrue((root / "dashboard.json").is_file())

    def test_queued_work_has_separate_threshold(self):
        task = {"status": "queued", "task_id": "queued", "created_at": watchdog.iso(self.now - timedelta(minutes=30))}
        result = watchdog.evaluate({"workers": [self.worker], "tasks": [task]}, now=self.now)
        self.assertTrue(result["healthy"])
        result = watchdog.evaluate({"workers": [self.worker], "tasks": [task]}, now=self.now, queue_seconds=600)
        self.assertIn("receiver:queue_overdue:queued", result["active"])

    def test_additional_task_file_with_stdin_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tasks.json").write_text(json.dumps([{"task_id": "old", "status": "queued", "created_at": "2020-01-01T00:00:00Z"}]))
            result = subprocess.run([sys.executable, str(MODULE), "--input", "-", "--state", str(root / "state.json"), "--tasks-file", str(root / "tasks.json"), "--dry-run", "--now", watchdog.iso(self.now)], input=json.dumps(self.worker), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("receiver:queue_overdue:old", json.loads(result.stdout)["active"])
            self.assertFalse((root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
