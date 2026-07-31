from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "local_ci"))

import bridge_gitee_to_github_status as bridge  # noqa: E402


class CodexCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = bridge.Target(
            "feature",
            "ci/pr-42/feature",
            "a" * 40,
            "PR #42 feature",
        )
        self.result = bridge.LocalCIResult(
            0,
            "https://gitee.example/results/run",
            "run-1",
            "pass",
            "pass",
            "pass",
            {},
            "## Codex AI 关键问题概述\n\n发现一个问题。",
        )

    def test_pr_number_is_derived_only_from_pr_task_refs(self) -> None:
        self.assertEqual(bridge.pr_number_from_task_ref(self.target.task_ref), 42)
        self.assertIsNone(
            bridge.pr_number_from_task_ref("ci/push/feature")
        )

    def test_comment_body_contains_summary_link_and_stable_marker(self) -> None:
        body = bridge.codex_pr_comment_body(self.target, self.result)
        self.assertIn("发现一个问题。", body)
        self.assertIn("- 提交：", body)
        self.assertIn("`aaaaaaaaaaaa`", body)
        self.assertIn(self.result.target_url, body)
        self.assertIn(bridge.CODEX_COMMENT_MARKER, body)

    @mock.patch.object(bridge, "request_json")
    @mock.patch.object(bridge, "get_github_json", return_value=[])
    def test_new_pr_comment_is_created(
        self, get_json: mock.Mock, request_json: mock.Mock
    ) -> None:
        request_json.return_value = (201, {}, "")
        with mock.patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "token"},
        ):
            bridge.post_codex_pr_comment(self.target, self.result)

        get_json.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments", {"per_page": "100"}
        )
        self.assertEqual(request_json.call_args.kwargs["method"], "POST")

    @mock.patch.object(bridge, "request_json")
    @mock.patch.object(bridge, "get_github_json")
    def test_existing_codex_comment_is_updated(
        self, get_json: mock.Mock, request_json: mock.Mock
    ) -> None:
        get_json.return_value = [
            {"id": 99, "body": bridge.CODEX_COMMENT_MARKER, "user": {"type": "Bot"}}
        ]
        request_json.return_value = (200, {}, "")
        with mock.patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "token"},
        ):
            bridge.post_codex_pr_comment(self.target, self.result)

        self.assertEqual(request_json.call_args.kwargs["method"], "PATCH")
        self.assertIn("/issues/comments/99", request_json.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
