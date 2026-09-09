from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

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

    def test_mail_config_missing_disables_optional_delivery(self):
        self.assertIsNone(watchdog.smtp_configuration({}))
        self.assertIsNone(watchdog.smtp_configuration({"LOCAL_CI_SMTP_HOST": "", "LOCAL_CI_SMTP_PORT": "587",
                                                      "LOCAL_CI_SMTP_SSL": "0", "LOCAL_CI_SMTP_STARTTLS": "true"}))

    def test_partial_mail_config_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "configuration is incomplete"):
            watchdog.smtp_configuration({"LOCAL_CI_SMTP_HOST": "fixture.invalid"})

    def test_direct_delivery_without_smtp_does_not_claim_success(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "delivery is disabled"):
            watchdog.deliver({})

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

    def test_missing_smtp_preserves_incidents_and_recovery_without_mail_backlog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {key: value for key, value in os.environ.items() if not key.startswith("LOCAL_CI_SMTP_")}
            command = [sys.executable, str(MODULE), "--input", "-", "--state", str(root / "state.json"),
                       "--output", str(root / "dashboard.json"), "--now", watchdog.iso(self.now)]
            result = subprocess.run(command, input=json.dumps({"workers": [], "expected_workers": ["worker"]}), env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual("disabled", state["mail_delivery"])
            self.assertTrue(state["active"])
            self.assertFalse(state["healthy"])
            self.assertFalse(state["pending_notifications"])
            self.assertEqual(state, json.loads((root / "dashboard.json").read_text()))
            healthy = {"workers": [{**self.worker, "worker_id": "worker"}], "expected_workers": ["worker"]}
            for _ in range(2):
                recovered = subprocess.run(command, input=json.dumps(healthy), env=env, text=True, capture_output=True)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertTrue(state["healthy"])
            self.assertFalse(state["pending_notifications"])
            self.assertEqual(["opened", "recovered"], [entry["transition"] for entry in state["history"]])

    def test_partial_smtp_preserves_pending_and_dashboard_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [str(MODULE), "--input", "-", "--state", str(root / "state.json"),
                    "--output", str(root / "dashboard.json"), "--now", watchdog.iso(self.now)]
            with patch.object(sys, "argv", argv), patch.object(sys, "stdin", io.StringIO(json.dumps({"workers": [], "expected_workers": ["worker"]}))), \
                 patch.dict(os.environ, {"LOCAL_CI_SMTP_HOST": "fixture.invalid"}, clear=True), patch.object(watchdog.smtplib, "SMTP") as smtp:
                self.assertEqual(1, watchdog.main())
                smtp.assert_not_called()
            state = json.loads((root / "state.json").read_text())
            self.assertTrue(state["pending_notifications"])
            self.assertEqual(state, json.loads((root / "dashboard.json").read_text()))

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

    def test_workspace_error_deduplicates_and_recovers_only_with_healthy_snapshot(self):
        worker = {**self.worker, "workspaces": {"status": "error", "logical_bytes": 101, "max_bytes": 100}}
        state = watchdog.evaluate(worker, now=self.now)
        self.assertEqual({"worker-1:workspace_cleanup_failed"}, set(state["active"]))
        self.assertEqual(1, len(state["pending_notifications"]))
        state["pending_notifications"] = []
        repeated = watchdog.evaluate(worker, state, now=self.now)
        self.assertFalse(repeated["pending_notifications"])
        for unavailable in (self.worker, {**self.worker, "workspaces": {"status": "unreported"}}):
            missing = watchdog.evaluate(unavailable, repeated, now=self.now)
            self.assertEqual(set(repeated["active"]), set(missing["active"]))
            self.assertFalse(missing["pending_notifications"])
        restored = watchdog.evaluate({**self.worker, "workspaces": {"status": "healthy"}}, repeated, now=self.now)
        self.assertTrue(restored["healthy"])
        self.assertEqual(["recovered"], [row["transition"] for row in restored["pending_notifications"]])

    def test_quarantine_alerts_per_generation_until_stop_is_confirmed(self):
        g1 = {"generation": "g1", "state": "quarantined", "stopped": False}
        g2 = {"generation": "g2", "state": "quarantined"}
        worker = {**self.worker, "environments": {"active": {}, "generations": [g1, g2]}}
        first = watchdog.evaluate(worker, now=self.now)
        self.assertEqual({"worker-1:environment_quarantine_unconfirmed:g1", "worker-1:environment_quarantine_unconfirmed:g2"}, set(first["active"]))
        first["pending_notifications"] = []
        self.assertFalse(watchdog.evaluate(worker, first, now=self.now)["pending_notifications"])
        for missing in (self.worker, {**self.worker, "environments": {"error": "unreadable", "generations": []}}):
            state = watchdog.evaluate(missing, first, now=self.now)
            self.assertTrue(set(first["active"]).issubset(state["active"]))
            self.assertFalse(any(row["transition"] == "recovered" for row in state["pending_notifications"]))
        g1["stopped"] = True
        partial = watchdog.evaluate(worker, first, now=self.now)
        self.assertEqual({"worker-1:environment_quarantine_unconfirmed:g2"}, set(partial["active"]))
        self.assertEqual("g1", partial["pending_notifications"][0]["incident"]["generation"])
        self.assertEqual("recovered", partial["pending_notifications"][0]["transition"])
        partial["pending_notifications"] = []
        g2["stopped"] = True
        restored = watchdog.evaluate(worker, partial, now=self.now)
        self.assertTrue(restored["healthy"])
        self.assertEqual("g2", restored["pending_notifications"][0]["incident"]["generation"])

    def test_dirty_or_safely_stopped_generation_is_not_an_unconfirmed_quarantine(self):
        worker = {**self.worker, "environments": {"active": {}, "generations": [
            {"generation": "g1", "state": "dirty"},
            {"generation": "g2", "state": "quarantined", "stopped": True},
        ]}}
        self.assertTrue(watchdog.evaluate(worker, now=self.now)["healthy"])

    def test_dashboard_summary_preserves_budget_and_states_without_private_values(self):
        worker = {**self.worker, "host_path": "/private/host", "config": {"token": "secret-value"},
                  "workspaces": {"status": "error", "logical_bytes": 200, "max_bytes": 100,
                                 "state_free_bytes": 10, "minimum_free_bytes": 20, "durable_evidence_bytes": 30,
                                 "root": "/private/scratch", "errors": [{"task_id": "t1", "error": "Permission denied: /private/task"}],
                                 "workspaces": [{"task_id": "t1", "generation": "g1", "phase": "cleanup_failed", "reason": "cleanup_timeout", "path": "/private/row"}]},
                  "environments": {"active": {}, "config_path": "/private/config", "generations": [
                      {"generation": "g1", "state": "quarantined", "stopped": False, "running": True,
                       "reusable": False, "quarantine_reason": "task_process_stop_failed", "env": {"TOKEN": "secret-value"}, "workspace_host": "/private/workspace"}]}}
        result = watchdog.evaluate(worker, now=self.now)
        summary = result["worker_health"][0]
        self.assertEqual(30, summary["workspaces"]["durable_evidence_bytes"])
        self.assertEqual(10, summary["workspaces"]["state_free_bytes"])
        self.assertEqual(20, summary["workspaces"]["minimum_free_bytes"])
        self.assertEqual("cleanup_timeout", summary["workspaces"]["workspaces"][0]["reason"])
        self.assertEqual("details_available_on_worker", summary["workspaces"]["errors"][0]["reason"])
        self.assertEqual("quarantined", summary["environments"]["generations"][0]["state"])
        self.assertFalse(summary["environments"]["generations"][0]["stopped"])
        self.assertNotIn("/private", json.dumps(result))
        self.assertNotIn("secret-value", json.dumps(result))

    def test_dashboard_summary_exposes_rootless_images_and_attempts_without_endpoint(self):
        worker = {**self.worker, "runtime": {"kind": "docker-rootless", "rootless": True, "available": False,
                  "endpoint": "unix:///run/user/1001/private.sock", "error": "secret runtime failure"},
                  "images": [{"release_id": "release-1", "image_id": "sha256:" + "a" * 64, "state": "active", "validated": True,
                              "source": "private-registry.invalid/image", "env": {"TOKEN": "secret"}}],
                  "task_containers": [{"task_id": "t1", "run_id": "run-1", "attempt_id": "attempt-1", "state": "retained",
                                       "stopped": True, "image_id": "sha256:" + "a" * 64, "workspace_host": "/private/workspace"}]}
        public = watchdog.evaluate(worker, now=self.now)["worker_health"][0]
        self.assertTrue(public["runtime"]["rootless"])
        self.assertTrue(public["runtime"]["unavailable"])
        self.assertTrue(public["images"][0]["validated"])
        self.assertEqual("attempt-1", public["task_containers"][0]["attempt_id"])
        self.assertTrue(public["task_containers"][0]["stopped"])
        for forbidden in ("private", "secret", "TOKEN", "endpoint"):
            self.assertNotIn(forbidden, json.dumps(public))

    def test_cli_cleanup_alerts_and_recovery_generate_one_mail_per_transition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, str(MODULE), "--input", "-", "--state", str(root / "state.json"),
                       "--mail-outbox", str(root / "outbox"), "--output", str(root / "dashboard.json"), "--now", watchdog.iso(self.now)]
            worker = {**self.worker, "workspaces": {"status": "error"},
                      "environments": {"active": {}, "generations": [{"generation": "g1", "state": "quarantined", "stopped": False}]}}
            for _ in range(2):
                result = subprocess.run(command, input=json.dumps(worker), text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(2, len(list((root / "outbox").glob("*.eml"))))
            worker["workspaces"]["status"] = "healthy"
            worker["environments"]["generations"][0]["stopped"] = True
            for _ in range(2):
                result = subprocess.run(command, input=json.dumps(worker), text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)
            messages = [BytesParser(policy=policy.default).parsebytes(path.read_bytes()) for path in (root / "outbox").glob("*.eml")]
            self.assertEqual(4, len(messages))
            self.assertEqual(2, sum("环境代际：g1" in message.get_content() for message in messages))
            state = json.loads((root / "state.json").read_text())
            self.assertTrue(state["healthy"])
            self.assertFalse(state["pending_notifications"])
            self.assertEqual(state["worker_health"], json.loads((root / "dashboard.json").read_text())["worker_health"])

    def test_fake_smtp_failure_keeps_cleanup_notification_for_retry(self):
        class FakeSMTP:
            fail = True
            sent = []
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def send_message(self, message):
                if self.fail:
                    raise watchdog.smtplib.SMTPException("fixture send failed")
                self.sent.append(message)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [str(MODULE), "--input", "-", "--state", str(root / "state.json"), "--now", watchdog.iso(self.now)]
            smtp_env = {"LOCAL_CI_SMTP_HOST": "fixture.invalid", "LOCAL_CI_SMTP_FROM": "sender@fixture.invalid",
                        "LOCAL_CI_SMTP_TO": "maintainer@fixture.invalid", "LOCAL_CI_SMTP_STARTTLS": "false"}
            worker = {**self.worker, "workspaces": {"status": "error"}}
            def run():
                with patch.object(sys, "argv", command), patch.object(sys, "stdin", io.StringIO(json.dumps(worker))), \
                     patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()), \
                     patch.dict(os.environ, smtp_env, clear=True), patch.object(watchdog.smtplib, "SMTP", FakeSMTP):
                    return watchdog.main()
            self.assertEqual(1, run())
            prior = json.loads((root / "state.json").read_text())
            self.assertEqual(1, len(prior["pending_notifications"]))
            notification_id = prior["pending_notifications"][0]["id"]
            FakeSMTP.fail = False
            self.assertEqual(0, run())
            self.assertEqual(0, run())
            self.assertEqual(1, len(FakeSMTP.sent))
            self.assertEqual(f"<{notification_id}@local-ci.invalid>", FakeSMTP.sent[0]["Message-ID"])
            self.assertFalse(json.loads((root / "state.json").read_text())["pending_notifications"])


if __name__ == "__main__":
    unittest.main()
