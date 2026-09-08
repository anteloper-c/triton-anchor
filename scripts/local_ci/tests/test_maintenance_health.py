"""Operational fault detection and notification delivery contracts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts.local_ci.maintenance.health import collect
from scripts.local_ci.maintenance.watchdog import evaluate
from scripts.local_ci.maintenance.workers import WorkerError, atomic_json


class HealthAndNotification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"worker_id": "test-host", "state_dir": str(self.root), "profiles": [{"id": "3.0"}],
                       "health": {"min_free_gb": 0}}
        self.manager = Mock()
        self.manager.inspect.return_value = {"running": True}
        atomic_json(self.root / "health" / "poller.json", {"heartbeat_at": 1000, "state": "idle"})

    def test_healthy_then_poller_stale_and_task_stall(self):
        self.assertEqual(collect(self.config, self.manager, now=1000)["state"], "healthy")
        atomic_json(self.root / "health" / "task.json", {"state": "running", "started_at": 0, "heartbeat_at": 500})
        snapshot = collect(self.config, self.manager, now=15000)
        codes = {i["code"] for i in snapshot["issues"]}
        self.assertTrue({"poller_stale", "task_stalled", "task_overdue"}.issubset(codes))

    def test_docker_failure_and_publish_pending_are_visible(self):
        self.manager.inspect.side_effect = WorkerError("offline")
        atomic_json(self.root / "health" / "poller.json", {"heartbeat_at": 1000, "state": "publish_pending"})
        snapshot = collect(self.config, self.manager, now=1000)
        self.assertEqual({i["code"] for i in snapshot["issues"]}, {"docker_unavailable", "poller_publish_pending"})

    def test_health_has_no_mail_or_issue_credentials_requirement(self):
        snapshot = collect(self.config, self.manager, now=1000)
        self.assertEqual(snapshot["state"], "healthy")
        self.assertEqual(snapshot["issues"], [])
        self.assertNotIn("notification_config", snapshot)

    def test_independent_watchdog_detects_whole_host_silence(self):
        snapshot = collect(self.config, self.manager, now=1000)
        self.assertEqual(evaluate(snapshot, "test-host", now=1100), [])
        self.assertEqual(evaluate(snapshot, "test-host", now=2000)[0]["code"], "host_offline")
        self.assertEqual(evaluate(snapshot, "different-host", now=1100)[0]["code"], "heartbeat_invalid")

    def test_watchdog_carries_current_local_faults(self):
        self.manager.inspect.return_value = {"running": False, "oom_killed": True}
        snapshot = collect(self.config, self.manager, now=1000)
        codes = {i["code"] for i in evaluate(snapshot, "test-host", now=1100)}
        self.assertEqual(codes, {"container_unavailable", "container_oom"})

    def test_corrupted_status_is_reported_instead_of_stopping_monitor(self):
        (self.root / "health" / "poller.json").write_text("{broken", encoding="utf-8")
        snapshot = collect(self.config, self.manager, now=1000)
        self.assertIn("poller_status_invalid", [i["code"] for i in snapshot["issues"]])

    def test_malformed_remote_snapshot_is_visible(self):
        self.assertEqual(evaluate([], "test-host", now=1000)[0]["code"], "heartbeat_invalid")
        snapshot = collect(self.config, self.manager, now=1000)
        snapshot["issues"] = ["not-an-issue"]
        self.assertEqual(evaluate(snapshot, "test-host", now=1000)[0]["code"], "heartbeat_invalid")


if __name__ == "__main__":
    unittest.main()
