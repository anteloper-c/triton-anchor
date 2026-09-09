"""Essential behavior checks for related CI responsibilities."""
from __future__ import annotations

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

    def test_resources_are_sampled_together_and_missing_metrics_are_not_health_failures(self):
        self.manager.inspect.return_value = {"running": True, "container": "worker-3"}
        runner = Mock(return_value=Mock(returncode=0, stdout='{"Name":"worker-3","CPUPerc":"125%","MemUsage":"1GiB / 8GiB","PIDs":"7"}\n'))
        snapshot = collect(self.config, self.manager, now=1000, service_runner=runner)
        self.assertEqual(snapshot['workers'][0]['resources']['cpu_percent'], '125%')
        self.assertEqual(runner.call_count, 1)
        self.assertIn('--no-stream', runner.call_args.args[0])
        self.manager.inspect.return_value = {"running": True, "container": "worker-3"}
        runner.side_effect = OSError('metrics unavailable')
        snapshot = collect(self.config, self.manager, now=1000, service_runner=runner)
        self.assertEqual(snapshot['state'], 'healthy')
        self.assertNotIn('resources', snapshot['workers'][0])

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

import copy

import io

import json

import os

from unittest.mock import patch

from urllib.error import HTTPError

from scripts.local_ci.maintenance.notify import GitHubIssue, Notifier, NotificationError, START

class IssueAPI:
    def __init__(self):
        self.issue = {"state": "open", "body": "维护者的说明，请保留。", "comments": 0}
        self.comments, self.calls = [], []
        self.fail, self.lose_post_response = None, False

    def call(self, method, suffix="", document=None):
        self.calls.append((method, suffix, document))
        if method == self.fail:
            raise NotificationError("GitHub 通知暂时不可用。")
        if method == "GET" and not suffix:
            return {**self.issue, "comments": len(self.comments)}
        if method == "GET":
            page = int(suffix.rsplit("=", 1)[1])
            return copy.deepcopy(self.comments[(page - 1) * 100:page * 100])
        if method == "POST":
            comment = {"body": document["body"], "user": {"login": "github-actions[bot]", "type": "Bot"}}
            self.comments.append(comment)
            if self.lose_post_response:
                self.lose_post_response = False
                raise NotificationError("GitHub 请求结果暂不确定。")
            return copy.deepcopy(comment)
        if method == "PATCH":
            self.issue.update(document)
            return copy.deepcopy(self.issue)
        raise AssertionError(method)

class IssueNotifications(unittest.TestCase):
    def setUp(self):
        self.api = IssueAPI()
        self.notifier = Notifier({}, api=self.api)
        self.issues = [{"code": "poller_stale", "message": "secret-host /private/path internal-42"}]

    def update(self, issues=None, **kwargs):
        return self.notifier.update("private-worker-host", self.issues if issues is None else issues, **kwargs)

    def test_incident_changed_fault_and_recovery_only_comment_once_each(self):
        self.assertEqual(self.update()["status"], "sent")
        self.issues[0]["message"] += " fluctuating duration"
        # New runner, no local notification cache.
        self.notifier = Notifier({}, api=self.api)
        self.assertEqual(self.update()["status"], "unchanged")
        self.issues.append({"code": "container_oom", "profile_id": "triton-3.0", "message": "private"})
        self.assertEqual(self.update()["status"], "sent")
        self.assertEqual(self.update(list(reversed(self.issues)))["status"], "unchanged")
        self.assertEqual(self.update([])["status"], "sent")
        self.assertEqual(self.update([])["status"], "unchanged")
        self.assertEqual(len(self.api.comments), 3)
        self.assertIn("已恢复", self.api.issue["body"])
        self.assertIn("维护者的说明，请保留。", self.api.issue["body"])

    def test_first_healthy_baseline_and_same_heartbeat_never_comment_or_patch_again(self):
        self.assertEqual(self.update([])["status"], "healthy")
        mutations = len([c for c in self.api.calls if c[0] != "GET"])
        self.assertEqual(self.update([])["status"], "unchanged")
        self.assertEqual(len(self.api.comments), 0)
        self.assertEqual(len([c for c in self.api.calls if c[0] != "GET"]), mutations)

    def test_post_failure_retries_recovery_without_losing_incident(self):
        self.update()
        self.api.fail = "POST"
        with self.assertRaises(NotificationError):
            self.update([])
        self.assertIn("需要处理", self.api.issue["body"])
        self.api.fail = None
        self.assertEqual(self.update([])["status"], "sent")
        self.assertEqual(len(self.api.comments), 2)

    def test_lost_post_response_and_failed_body_patch_do_not_duplicate_comments(self):
        for failure in ("lost-post-response", "PATCH"):
            with self.subTest(failure=failure):
                self.setUp()
                self.api.lose_post_response = failure == "lost-post-response"
                self.api.fail = "PATCH" if failure == "PATCH" else None
                with self.assertRaises(NotificationError):
                    self.update()
                self.assertEqual(len(self.api.comments), 1)
                self.api.fail = None
                self.assertEqual(self.update()["status"], "unchanged")
                self.assertEqual(len(self.api.comments), 1)
                self.assertIn("需要处理", self.api.issue["body"])

    def test_untrusted_comment_cannot_forge_delivery_and_pagination_finds_bot(self):
        self.update()
        fake_recovery = self.update([], dry_run=True)["body"]
        self.api.comments.extend({"body": fake_recovery, "user": {"login": "contributor", "type": "User"}} for _ in range(205))
        self.assertEqual(self.update([])["status"], "sent")
        self.assertEqual(len(self.api.comments), 207)
        self.assertTrue(any(c[1].endswith("page=3") for c in self.api.calls))

    def test_public_comments_and_body_do_not_expose_host_details_or_raw_codes(self):
        self.issues += [{"code": "internal-private-code", "message": "me@private.invalid",
                         "path": "/private/path", "service": "secret.service", "profile_id": "secret-profile"},
                        {"code": "maintenance_failed", "profile_id": "triton-3.6", "message": "secret"}]
        self.update()
        public = self.api.issue["body"] + "".join(c["body"] for c in self.api.comments)
        for private in ("private", "secret", "poller_stale", "maintenance_failed", "me@"):
            self.assertNotIn(private, public)
        self.assertIn("Triton 3.6", public)

    def test_dry_run_never_reads_or_writes_issue(self):
        self.assertEqual(self.update(dry_run=True)["status"], "dry_run")
        self.assertEqual(self.api.calls, [])

    def test_closed_pr_and_malformed_body_fail_before_comment(self):
        for change in ({"state": "closed"}, {"pull_request": {}}, {"body": START + " broken"}):
            with self.subTest(change=change):
                self.setUp()
                self.api.issue.update(change)
                with self.assertRaises(NotificationError):
                    self.update()
                self.assertEqual(self.api.comments, [])

class GitHubTransport(unittest.TestCase):
    def test_missing_issue_wrong_repository_and_token_have_actionable_errors(self):
        for config in ({}, {"repository": "other/repository", "issue_number": 1},
                       {"repository": "anteloper-c/triton-anchor", "issue_number": True}):
            with self.subTest(config=config), self.assertRaisesRegex(NotificationError, "LOCAL_CI_OPERATIONS_ISSUE_NUMBER"):
                GitHubIssue(config)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(NotificationError, "issues:write"):
            GitHubIssue({"repository": "anteloper-c/triton-anchor", "issue_number": 7})

    @patch("scripts.local_ci.maintenance.notify.request.build_opener")
    def test_fixed_https_target_header_token_and_sanitized_api_failure(self, opener):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-test-token"}):
            api = GitHubIssue({"repository": "anteloper-c/triton-anchor", "issue_number": 7})
        opener.return_value.open.return_value = io.BytesIO(json.dumps({"state": "open"}).encode())
        self.assertEqual(api.call("GET"), {"state": "open"})
        call = opener.return_value.open.call_args.args[0]
        self.assertEqual(call.full_url, "https://api.github.com/repos/anteloper-c/triton-anchor/issues/7")
        self.assertEqual(call.get_header("Authorization"), "Bearer private-test-token")
        opener.return_value.open.side_effect = HTTPError(call.full_url, 403, "private-test-token", {}, None)
        with self.assertRaises(NotificationError) as caught:
            api.call("POST", "/comments", {"body": "故障"})
        self.assertNotIn("private-test-token", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

import contextlib

import textwrap

import time

from urllib.error import URLError

from scripts.local_ci.maintenance import watchdog

from scripts.local_ci.maintenance.notify import Notifier


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
