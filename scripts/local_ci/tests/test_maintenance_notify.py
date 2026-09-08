"""GitHub Issue state transitions use a fake API; never post real comments."""
import copy
import io
import json
import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
