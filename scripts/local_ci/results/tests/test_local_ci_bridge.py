from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "local_ci" / "results"))

import bridge_gitee_to_github_status as bridge


class BridgeArgumentTests(unittest.TestCase):
    def parse(self, *extra: str):
        argv = [
            "bridge",
            "--gitee-owner",
            "owner",
            "--gitee-repo",
            "results",
            "--gitee-web-url",
            "https://gitee.example/results",
            *extra,
        ]
        with mock.patch.object(sys, "argv", argv):
            return bridge.parse_args()

    def test_source_branch_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            self.parse()

    def test_status_sha_cannot_differ_from_tested_sha(self) -> None:
        with self.assertRaises(SystemExit):
            self.parse(
                "--source-branch",
                "feature/demo",
                "--sha",
                "a" * 40,
                "--status-sha",
                "b" * 40,
            )

    def test_status_sha_requires_tested_sha(self) -> None:
        with self.assertRaises(SystemExit):
            self.parse(
                "--source-branch",
                "feature/demo",
                "--status-sha",
                "a" * 40,
            )

    def test_status_sha_may_equal_tested_sha(self) -> None:
        args = self.parse(
            "--source-branch",
            "feature/demo",
            "--sha",
            "a" * 40,
            "--status-sha",
            "a" * 40,
        )
        self.assertEqual(args.source_branch, "feature/demo")


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
                    "## Codex AI 代码审查\n\n"
                    "> 这是非阻塞的辅助审查；确定性 CI 结果仍是合入门禁。\n\n"
                    "### 审查摘要\n\n发现一个问题。\n\nCodex AI CI 已完成，Local CI 已通过。\n\n"
                    "### 贡献者目标与实现情况\n\n实现不完整。\n\n"
                    "### 需要处理的问题\n\n#### 1. [中风险] 示例问题\n\n"
                    "### 验证情况\n\nRUN-001 稳定复现问题。\n\n"
                    "### 变更文件\n\n<details></details>"
                ),
                "https://gitee.example/results/run/codex-ai-report.md",
                (
                    bridge.FindingLocation(
                        "AI-001", "python/example.py", "17-18"
                    ),
                ),
                "",
                (("RUN-001", "缓存版本失配定向测试"),),
            ),
        )

    def test_pr_number_is_derived_only_from_pr_task_refs(self) -> None:
        self.assertEqual(bridge.pr_number_from_task_ref(self.target.task_ref), 42)
        self.assertIsNone(bridge.pr_number_from_task_ref("ci/push/feature"))

    def test_validation_purpose_is_read_from_report_and_used_in_comment(self) -> None:
        report_json = json.dumps(
            {
                "test_execution": {
                    "commands": [
                        {"id": "RUN-001", "purpose": "Python 语法检查"}
                    ]
                }
            }
        )
        purposes = bridge.validation_purposes_from_report(report_json)

        self.assertEqual(purposes, (("RUN-001", "Python 语法检查"),))
        self.assertEqual(
            bridge.public_comment_text("RUN-001 已通过", purposes),
            "Python 语法检查已通过",
        )
        self.assertEqual(
            bridge.public_comment_text("RUN-001 已通过"),
            "相关验证已通过",
        )

    def test_comment_body_contains_summary_link_and_stable_marker(self) -> None:
        body = bridge.codex_pr_comment_body(self.target, self.result)
        self.assertIn("发现一个问题。", body)
        self.assertIn("## Codex AI 自动审查", body)
        self.assertIn("### 审查摘要", body)
        self.assertIn("### 贡献者目标与实现情况", body)
        self.assertIn("### 变更文件", body)
        self.assertIn("- 测试提交：", body)
        self.assertIn("`aaaaaaaaaaaa`", body)
        self.assertIn("Codex 自动审查状态：完成", body)
        self.assertIn("结论：警告", body)
        self.assertIn("补充验证结果：存在可稳定复现的失败", body)
        self.assertIn("缓存版本失配定向测试稳定复现问题", body)
        self.assertNotIn("RUN-001", body)
        self.assertIn("Codex AI 自动审查已完成，本地确定性 CI 检查已通过", body)
        self.assertNotIn("Codex AI CI", body)
        self.assertNotIn("Local CI", body)
        self.assertIn("查看完整 Codex AI 自动审查报告", body)
        self.assertIn(self.result.codex_ai.report_url, body)
        self.assertIn(bridge.CODEX_COMMENT_MARKER, body)
        self.assertIn(bridge.codex_pr_commit_marker(self.target), body)

    def test_comment_body_links_findings_to_the_reviewed_commit(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            body = bridge.codex_pr_comment_body(self.target, self.result)

        self.assertIn("### 可点击代码定位", body)
        self.assertIn("提交者修复和审核者核对代码功能", body)
        self.assertIn("问题 1：", body)
        self.assertNotIn("AI-001", body)
        self.assertLess(
            body.index("### 可点击代码定位"),
            body.index("### 验证情况"),
        )
        self.assertIn(
            "https://github.com/owner/repo/blob/"
            f"{self.target.sha}/python/example.py#L17-L18",
            body,
        )

    def test_fork_pr_location_uses_head_repository(self) -> None:
        fork_target = bridge.Target(
            self.target.source_branch,
            self.target.task_ref,
            self.target.sha,
            self.target.label,
            "fork-owner/fork-repo",
        )
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            body = bridge.codex_pr_comment_body(fork_target, self.result)

        self.assertIn("https://github.com/fork-owner/fork-repo/blob/", body)

    @mock.patch.object(bridge, "get_github_json")
    def test_reconcile_pr_targets_use_test_merge_sha(self, get_json: mock.Mock) -> None:
        get_json.return_value = [
            {
                "number": 42,
                "head": {
                    "ref": "feature",
                    "sha": "a" * 40,
                    "repo": {"full_name": "owner/repo"},
                },
                "merge_commit_sha": "b" * 40,
            }
        ]
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            targets = bridge.list_open_pr_targets(10)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].sha, "b" * 40)
        self.assertEqual(targets[0].task_ref, "ci/pr-42/feature")

    @mock.patch.object(bridge, "get_github_json")
    def test_reconcile_pr_targets_do_not_fallback_to_head_sha(
        self, get_json: mock.Mock
    ) -> None:
        get_json.return_value = [
            {
                "number": 42,
                "head": {
                    "ref": "feature",
                    "sha": "a" * 40,
                    "repo": {"full_name": "owner/repo"},
                },
                "merge_commit_sha": None,
            }
        ]
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            self.assertEqual(bridge.list_open_pr_targets(10), [])

    @mock.patch.object(bridge, "get_github_json")
    def test_current_pr_matches_frozen_merge_result(self, get_json: mock.Mock) -> None:
        get_json.return_value = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            # The PR API base SHA may lag behind the base parent frozen in the
            # GitHub merge-result; freshness is bound to head/ref/merge SHA.
            "base": {"sha": "e" * 40, "ref": "CI_dev"},
            "merge_commit_sha": "c" * 40,
        }
        args = SimpleNamespace(
            pr_number="42",
            expected_head_sha="a" * 40,
            comparison_base_sha="b" * 40,
            target_branch="CI_dev",
        )
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            self.assertTrue(bridge.current_pr_matches(args, "c" * 40))
            self.assertFalse(bridge.current_pr_matches(args, "d" * 40))

    @mock.patch.object(bridge, "get_github_json")
    def test_current_pr_rejects_force_push_and_retarget(self, get_json: mock.Mock) -> None:
        get_json.return_value = {
            "state": "open",
            "draft": False,
            "head": {"sha": "d" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
            "merge_commit_sha": "c" * 40,
        }
        args = SimpleNamespace(
            pr_number="42",
            expected_head_sha="a" * 40,
            comparison_base_sha="b" * 40,
            target_branch="CI_dev",
        )
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            self.assertFalse(bridge.current_pr_matches(args, "c" * 40))

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
    def test_existing_codex_comment_for_same_commit_is_updated(
        self, get_json: mock.Mock, request_json: mock.Mock
    ) -> None:
        get_json.return_value = [
            {
                "id": 99,
                "body": (
                    f"{bridge.CODEX_COMMENT_MARKER}\n"
                    f"{bridge.codex_pr_commit_marker(self.target)}"
                ),
                "user": {"type": "Bot"},
            }
        ]
        request_json.return_value = (200, {}, "")
        with mock.patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "token"},
        ):
            bridge.post_codex_pr_comment(self.target, self.result)

        self.assertEqual(request_json.call_args.kwargs["method"], "PATCH")
        self.assertIn("/issues/comments/99", request_json.call_args.args[0])

    @mock.patch.object(bridge, "request_json")
    @mock.patch.object(bridge, "get_github_json")
    def test_existing_codex_comment_for_different_commit_is_not_updated(
        self, get_json: mock.Mock, request_json: mock.Mock
    ) -> None:
        other_target = bridge.Target(
            self.target.source_branch,
            self.target.task_ref,
            "b" * 40,
            self.target.label,
        )
        get_json.return_value = [
            {
                "id": 99,
                "body": (
                    f"{bridge.CODEX_COMMENT_MARKER}\n"
                    f"{bridge.codex_pr_commit_marker(other_target)}"
                ),
                "user": {"type": "Bot"},
            }
        ]
        request_json.return_value = (201, {}, "")
        with mock.patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "token"},
        ):
            bridge.post_codex_pr_comment(self.target, self.result)

        self.assertEqual(request_json.call_args.kwargs["method"], "POST")
        self.assertIn("/issues/42/comments", request_json.call_args.args[0])

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
            (
                changed(execution_status="fail", failure_code="timeout"),
                "Codex 自动审查执行超时",
            ),
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
            payload["finding_locations"],
            [{"id": "AI-001", "file": "python/example.py", "line": "17-18"}],
        )
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
            if path.endswith("/publish-manifest.json"):
                return json.dumps(
                    {
                        "schema": "triton-anchor-local-ci-publish-manifest/v1",
                        "status": "passed",
                        "target_sha": self.target.sha,
                        "tested_sha": self.target.sha,
                        "run_id": "run-1",
                        "missing_expected_files": [],
                        "fallback": False,
                    }
                )
            if path.endswith("/delivery-summary.txt"):
                return (
                    "status: 0\n"
                    "frontend_build_status: pass\n"
                    "frontend_smoke_status: pass\n"
                    "backend_rebuild_status: pass\n"
                    "backend_smoke_jit_status: pass\n"
                    "flaggems_status: disabled\n"
                    "compile_time_status: disabled\n"
                    "pass_profile_status: disabled\n"
                    "ir_serialization_status: disabled\n"
                    "target_sha: " + self.target.sha + "\n"
                    "run_id: run-1\n"
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
                    "## Codex AI 代码审查\n\n"
                    "### 审查摘要\n\n发现一个问题。\n\n"
                    "### 贡献者目标与实现情况\n\n实现不完整。\n\n"
                    "### 需要处理的问题\n\n1. 示例问题。\n\n"
                    "### 验证情况\n\n定向测试稳定复现。\n\n"
                    "### 变更文件\n\n<details></details>\n"
                )
            if path.endswith("/codex-ai-report.json"):
                return json.dumps(
                    {
                        "findings": [
                            {
                                "id": "AI-001",
                                "file": "python/example.py",
                                "line": "17",
                            }
                        ]
                    }
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
        self.assertEqual(
            result.codex_ai.finding_locations,
            (bridge.FindingLocation("AI-001", "python/example.py", "17"),),
        )
        self.assertIsNotNone(result.publish_manifest)
        assert result.publish_manifest is not None
        self.assertEqual(result.publish_manifest.target_sha, self.target.sha)

    @mock.patch.object(bridge, "gitee_content")
    def test_success_is_forced_to_fail_when_required_stage_is_missing(
        self,
        gitee_content: mock.Mock,
    ) -> None:
        def content(*_args: object) -> str | None:
            path = str(_args[2])
            if path.endswith("latest.txt"):
                return "run-2\n"
            if path.endswith("delivery-summary.txt"):
                return (
                    "status: 0\n"
                    f"target_sha: {self.target.sha}\n"
                    "run_id: run-2\n"
                    "frontend_build_status: pass\n"
                    "frontend_smoke_status: pass\n"
                    "backend_rebuild_status: pass\n"
                    "backend_smoke_jit_status: not_run\n"
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
        self.assertEqual(result.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
