from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "local_ci" / "results"))

import bridge_gitee_to_github_status as bridge


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
            bridge.CodexAIResult(
                "pass",
                "WARNING",
                "stable_failure",
                "full",
                "warning",
                "生成测试和执行命令均在限制内。",
                "",
                (
                    "## Codex AI 审核摘要\n\n"
                    "发现一个问题。\n\n"
                    "合入建议：**修复后合入。**\n\n"
                    "### 补充验证\n\n定向测试稳定复现。\n\n"
                    "### 需要重点关注的问题\n\n"
                    "1. **[中风险] 示例问题**\n\n"
                    "### 具体文件变更\n\n<details></details>"
                ),
                "https://gitee.example/results/run/codex-ai-report.md",
            ),
        )

    def test_pr_number_is_derived_only_from_pr_task_refs(self) -> None:
        self.assertEqual(bridge.pr_number_from_task_ref(self.target.task_ref), 42)
        self.assertIsNone(bridge.pr_number_from_task_ref("ci/push/feature"))

    def test_comment_body_contains_summary_link_and_stable_marker(self) -> None:
        body = bridge.codex_pr_comment_body(self.target, self.result)
        self.assertIn("发现一个问题。", body)
        self.assertIn("## Codex AI 审核摘要", body)
        self.assertIn("### 具体文件变更", body)
        self.assertIn("- 提交：", body)
        self.assertIn("`aaaaaaaaaaaa`", body)
        self.assertIn(self.result.codex_ai.report_url, body)
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

    def test_push_does_not_publish_pr_comment(self) -> None:
        push_target = bridge.Target(
            "feature",
            "ci/push/feature",
            "b" * 40,
            "branch feature",
        )
        with (
            mock.patch.object(bridge, "get_github_json") as get_json,
            mock.patch.object(bridge, "request_json") as request_json,
        ):
            bridge.post_codex_pr_comment(push_target, self.result)
        get_json.assert_not_called()
        request_json.assert_not_called()

    def test_codex_advisory_status_is_always_non_blocking(self) -> None:
        args = SimpleNamespace(context="local-ci/test")
        with mock.patch.object(bridge, "post_github_status") as post_status:
            bridge.post_codex_advisory_status(args, self.target, self.result)

        status_args = post_status.call_args.args
        self.assertEqual(status_args[0], self.target.sha)
        self.assertEqual(status_args[1], "success")
        self.assertEqual(status_args[2], "local-ci/test/codex-ai-advisory")
        self.assertIn("可稳定复现的失败", status_args[3])
        self.assertIn("非阻塞", status_args[3])
        self.assertEqual(status_args[4], self.result.codex_ai.report_url)

    def test_advisory_descriptions_cover_non_pass_states(self) -> None:
        base = self.result.codex_ai

        def changed(**values: str) -> bridge.CodexAIResult:
            payload = {
                name: getattr(base, name)
                for name in bridge.CodexAIResult.__dataclass_fields__
            }
            payload.update(values)
            return bridge.CodexAIResult(**payload)

        cases = (
            (changed(execution_status="fail"), "未完成"),
            (changed(execution_status="skipped"), "未运行"),
            (
                changed(verdict="PASS", test_status="insufficient_evidence"),
                "证据不足",
            ),
            (
                changed(verdict="PASS", test_status="stable_failure"),
                "可稳定复现的失败",
            ),
            (
                changed(verdict="PASS", test_status="flaky_failure"),
                "非确定性失败",
            ),
            (
                changed(verdict="PASS", test_status="infrastructure_failure"),
                "基础设施失败",
            ),
            (
                changed(verdict="PASS", test_status="test_generation_error"),
                "测试生成失败",
            ),
            (changed(verdict="FAIL", test_status="passed"), "失败"),
            (
                changed(
                    verdict="PASS",
                    test_status="passed",
                    constraint_status="warning",
                ),
                "约束警告",
            ),
            (
                changed(
                    verdict="PASS",
                    test_status="passed",
                    constraint_status="pass",
                ),
                "通过",
            ),
        )
        for codex_ai, expected in cases:
            with self.subTest(expected=expected):
                description = bridge.codex_advisory_description(codex_ai)
                self.assertIn(expected, description)
                self.assertIn("非阻塞", description)

    def test_codex_ai_output_is_single_line_json(self) -> None:
        encoded = bridge.codex_ai_output_json(self.result)
        self.assertNotIn("\n", encoded)
        payload = json.loads(encoded)
        self.assertEqual(payload["execution_status"], "pass")
        self.assertEqual(payload["verdict"], "WARNING")
        self.assertEqual(payload["test_status"], "stable_failure")
        self.assertIn("发现一个问题", payload["comment_markdown"])
        self.assertEqual(
            payload["report_url"],
            self.result.codex_ai.report_url,
        )

    def test_write_github_outputs_includes_codex_ai_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "github-output.txt"
            with mock.patch.dict(
                os.environ,
                {"GITHUB_OUTPUT": str(output_path)},
            ):
                bridge.write_github_outputs(self.result)

            values = dict(
                line.split("=", 1)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            )
        payload = json.loads(values["codex_ai_result"])
        self.assertEqual(payload["analysis_mode"], "full")
        self.assertEqual(payload["constraint_status"], "warning")

    @mock.patch.object(bridge, "gitee_content")
    def test_read_result_combines_result_summary_and_comment(
        self, gitee_content: mock.Mock
    ) -> None:
        def content(
            owner: str,
            repo: str,
            path: str,
            ref: str,
            token: str,
        ) -> str | None:
            del owner, repo, ref, token
            if path.endswith("/latest.txt"):
                return "run-1\n"
            if path.endswith("/delivery-summary.txt"):
                return (
                    "status: 0\n"
                    "frontend_smoke_status: pass\n"
                    "backend_smoke_jit_status: pass\n"
                    "flaggems_status: disabled\n"
                    "compile_time_status: disabled\n"
                    "pass_profile_status: disabled\n"
                    "ir_serialization_status: disabled\n"
                )
            if path.endswith("/result.json"):
                return json.dumps(
                    {
                        "codex_ai_ci_status": "pass",
                        "codex_ai_ci_mode": "full",
                        "codex_ai_ci_verdict": "PASS",
                        "codex_ai_test_status": "passed",
                    }
                )
            if path.endswith("/codex-ai-ci-summary.txt"):
                return (
                    "status: pass\n"
                    "analysis_mode: full\n"
                    "report_verdict: WARNING\n"
                    "test_execution_status: stable_failure\n"
                    "constraint_status: warning\n"
                    "constraint_reason: 定向测试数量符合约束。\n"
                    "failure_reason: \n"
                )
            if path.endswith("/codex-ai-comment.md"):
                return (
                    "## Codex AI 审核摘要\n\n发现一个问题。\n\n"
                    "合入建议：**修复后合入。**\n\n"
                    "### 补充验证\n\n定向测试稳定复现。\n\n"
                    "### 需要重点关注的问题\n\n1. 示例问题。\n\n"
                    "### 具体文件变更\n\n<details></details>\n"
                )
            return None

        gitee_content.side_effect = content
        args = SimpleNamespace(
            gitee_owner="owner",
            gitee_repo="results",
            gitee_results_branch="local-ci-results",
            gitee_web_url="https://gitee.example/results",
        )
        result = bridge.read_local_ci_result(args, self.target, "token")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.codex_ai.execution_status, "pass")
        self.assertEqual(result.codex_ai.verdict, "WARNING")
        self.assertEqual(result.codex_ai.test_status, "stable_failure")
        self.assertEqual(result.codex_ai.constraint_status, "warning")
        self.assertIn("/blob/local-ci-results/", result.codex_ai.report_url)
        self.assertTrue(result.codex_ai.report_url.endswith("codex-ai-report.md"))


if __name__ == "__main__":
    unittest.main()
