"""External heartbeat and public GitHub notification behavior; all APIs are fake."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

from scripts.local_ci.maintenance import watchdog
from scripts.local_ci.maintenance.notify import Notifier
from scripts.local_ci.tests.test_maintenance_notify import IssueAPI


class ExternalWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {"heartbeat_url": "https://example.invalid/heartbeat.json",
                       "worker_id": "worker-test", "state_dir": str(self.root / "state")}
        self.snapshot = {"schema": "triton-anchor-local-ci-worker-health", "worker_id": "worker-test",
                         "heartbeat_at": time.time(), "state": "healthy", "issues": []}
        self.api = IssueAPI()

    def run_watchdog(self, unavailable=False, configured=True):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        response = io.BytesIO(json.dumps(self.snapshot).encode())
        with contextlib.ExitStack() as stack:
            read = stack.enter_context(patch.object(watchdog, "urlopen", side_effect=URLError("private-server") if unavailable else None,
                                                   return_value=response))
            if configured:
                stack.enter_context(patch.object(watchdog, "Notifier", return_value=Notifier({}, api=self.api)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = watchdog.main(["--config", str(path)])
        read.assert_called_once()
        return code, json.loads((self.root / "state/watchdog-latest.json").read_text())

    def test_healthy_without_smtp_initializes_issue_without_comment(self):
        code, result = self.run_watchdog()
        self.assertEqual(code, 0)
        self.assertEqual(result["notification"]["status"], "healthy")
        self.assertEqual(result["issues"], [])
        self.assertEqual(self.api.comments, [])
        self.assertNotIn("notification_not_configured", json.dumps(result))

    def test_stale_wrong_worker_unreachable_and_local_faults_are_real_failures(self):
        cases = [({"heartbeat_at": time.time() - 10000}, False), ({"worker_id": "different"}, False),
                 ({}, True), ({"state": "degraded", "issues": [{"code": "poller_stale", "message": "private-path"}]}, False)]
        original = dict(self.snapshot)
        for changes, unavailable in cases:
            with self.subTest(changes=changes):
                self.snapshot = {**original, **changes}
                code, result = self.run_watchdog(unavailable)
                self.assertEqual(code, 1)
                self.assertEqual(result["notification"]["status"], "sent")
                self.assertTrue(result["issues"])
                self.assertNotIn("private", json.dumps(result))

    def test_missing_issue_configuration_is_actionable_without_fake_mail_incident(self):
        code, result = self.run_watchdog(configured=False)
        self.assertEqual(code, 1)
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["notification"]["status"], "error")
        self.assertIn("LOCAL_CI_OPERATIONS_ISSUE_NUMBER", result["notification"]["message"])
        self.assertIn("LOCAL_CI_WATCHDOG_ENABLED=false", result["notification"]["message"])

    def test_failed_issue_update_is_error_even_after_service_recovers(self):
        self.snapshot.update(state="degraded", issues=[{"code": "poller_stale", "message": "stopped"}])
        self.run_watchdog()
        self.snapshot.update(state="healthy", issues=[])
        self.api.fail = "POST"
        code, result = self.run_watchdog()
        self.assertEqual((code, result["notification"]["status"]), (1, "error"))
        self.api.fail = None
        code, result = self.run_watchdog()
        self.assertEqual((code, result["notification"]["status"]), (0, "sent"))
        self.assertEqual(len(self.api.comments), 2)


class WatchdogWorkflowPreparation(unittest.TestCase):
    def test_actual_preparation_requires_trusted_issue_and_has_no_mail_inputs(self):
        # Execute only the workflow's trusted preparation, without YAML package,
        # network calls or Issue mutation; also works in the minimal host Python.
        root = Path(__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/local-ci-watchdog.yml").read_text(encoding="utf-8")
        script = textwrap.dedent(workflow.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
        self.assertNotIn("SMTP", workflow)
        self.assertNotIn("EMAIL_", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertIn("      issues: write", workflow)
        for number in ("", "abc", "0", "7"):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as directory:
                env = {"WATCHDOG_URL": "https://example.invalid/heartbeat.json", "WATCHDOG_WORKER": "worker-test",
                       "WATCHDOG_MAX_AGE": "1800", "OPERATIONS_ISSUE": number, "RUNNER_TEMP": directory}
                with patch.dict(os.environ, env, clear=True):
                    if number != "7":
                        with self.assertRaisesRegex(SystemExit, "LOCAL_CI_OPERATIONS_ISSUE_NUMBER"):
                            exec(compile(script, "watchdog-workflow-prepare", "exec"), {})
                        continue
                    exec(compile(script, "watchdog-workflow-prepare", "exec"), {})
                config = json.loads((Path(directory) / "watchdog.json").read_text())
                self.assertEqual(config["github"], {"repository": "anteloper-c/triton-anchor", "issue_number": 7, "token_env": "GITHUB_TOKEN"})
                self.assertNotIn("smtp", config)


if __name__ == "__main__":
    unittest.main()
