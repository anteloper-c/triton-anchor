"""Operational fault detection and notification delivery contracts."""
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock

from scripts.local_ci.maintenance.health import collect
from scripts.local_ci.maintenance.notify import Notifier, recipients
from scripts.local_ci.maintenance.watchdog import evaluate
from scripts.local_ci.maintenance.workers import WorkerError, atomic_json


class HealthAndNotification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.smtp = {"host": "smtp.example.invalid", "from": "ci@example.invalid",
                     "accounts": ["heron-mc", "likehupochuan"],
                     "account_emails": {"heron-mc": "one@example.invalid", "likehupochuan": "two@example.invalid"}}
        self.config = {"worker_id": "test-host", "state_dir": str(self.root), "profiles": [{"id": "3.0"}],
                       "smtp": self.smtp, "health": {"min_free_gb": 0}}
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

    def test_gitee_usernames_are_not_guessed_as_email_addresses(self):
        self.config["smtp"] = {"accounts": ["heron-mc", "likehupochuan"]}
        with self.assertRaisesRegex(ValueError, "heron-mc, likehupochuan"):
            recipients(self.config["smtp"])
        snapshot = collect(self.config, self.manager, now=1000)
        self.assertIn("notification_not_configured", [i["code"] for i in snapshot["issues"]])

    def test_mail_failure_retries_then_deduplicates_and_notifies_recovery(self):
        send = Mock(side_effect=[smtplib.SMTPException("failure"), None, None])
        notifier = Notifier(self.root, self.smtp, send=send)
        issues = [{"code": "poller_stale", "message": "heartbeats stopped"}]
        self.assertEqual(notifier.update("test-host", issues)["status"], "pending")
        self.assertEqual(notifier.update("test-host", issues)["status"], "sent")
        issues[0]["message"] = "still stopped with a different duration"
        self.assertEqual(notifier.update("test-host", issues)["status"], "unchanged")
        self.assertEqual(notifier.update("test-host", [])["status"], "sent")
        self.assertEqual(notifier.update("test-host", [])["status"], "unchanged")
        self.assertEqual(send.call_count, 3)

    def test_dry_run_never_sends_or_marks_incident_delivered(self):
        send = Mock()
        notifier = Notifier(self.root, self.smtp, send=send)
        issues = [{"code": "disk_low", "message": "low disk"}]
        self.assertEqual(notifier.update("test-host", issues, dry_run=True)["status"], "dry_run")
        send.assert_not_called()
        self.assertEqual(notifier.update("test-host", issues)["status"], "sent")

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
