#!/usr/bin/env python3
"""Validate structured Codex output and render the fixed local-CI report."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SHARED_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPT_DIR))
from finding_locations import (  # noqa: E402
    MAX_FINDING_LINE_SPAN,
    normalized_repository_path,
    parse_finding_line_range as parse_shared_finding_line_range,
)

ROOT_KEYS = {
    "verdict",
    "summary",
    "merge_recommendation",
    "change_request_assessment",
    "changed_files",
    "behavior_coverage",
    "findings",
    "suggested_tests",
    "residual_risks",
    "test_execution",
    "completion_marker",
}
CHANGE_REQUEST_ASSESSMENT_KEYS = {
    "status",
    "contributor_goal",
    "expected_behavior",
    "implementation_summary",
    "evidence",
}
CHANGED_FILE_KEYS = {
    "path",
    "change_type",
    "summary",
    "impact",
    "validation_strategy",
}
MANIFEST_FILE_KEYS = {"path", "change_type"}
MANIFEST_RENAME_KEYS = {"path", "change_type", "previous_path"}
BEHAVIOR_COVERAGE_KEYS = {
    "normal",
    "boundary",
    "error",
    "compatibility",
    "integration",
}
BEHAVIOR_ITEM_KEYS = {"scope", "strategy", "result"}
FINDING_KEYS = {
    "id",
    "severity",
    "category",
    "file",
    "line",
    "code_role",
    "title",
    "evidence",
    "impact",
    "fix_direction",
}
TEST_KEYS = {"id", "priority", "target", "description"}
TEST_EXECUTION_KEYS = {
    "status",
    "summary",
    "generated_test_files",
    "commands",
}
COMMAND_KEYS = {
    "id",
    "command",
    "exit_code",
    "duration_seconds",
    "status",
    "evidence",
    "purpose",
}
SEVERITIES = {"HIGH", "MEDIUM", "LOW"}
TEST_EXECUTION_STATUSES = {
    "not_run",
    "passed",
    "stable_failure",
    "flaky_failure",
    "infrastructure_failure",
    "test_generation_error",
    "insufficient_evidence",
}
COMMAND_STATUSES = {
    "passed",
    "stable_failure",
    "flaky_failure",
    "infrastructure_failure",
    "not_executed",
}
CHANGE_REQUEST_ASSESSMENT_STATUSES = {
    "implemented",
    "partially_implemented",
    "not_implemented",
    "not_assessable",
    "not_applicable",
}
CHANGE_TYPES = {"modified", "added", "deleted", "renamed"}
CATEGORIES = {
    "algorithm",
    "business-logic",
    "state-management",
    "cache-consistency",
    "concurrency",
    "resource-lifecycle",
    "data-integrity",
    "correctness",
    "regression",
    "security",
    "api-compatibility",
    "performance",
    "test-gap",
    "other",
}
CHINESE_TEXT_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
MAX_ASSESSMENT_EVIDENCE_ITEMS = 8
MAX_TEST_EXECUTION_SUMMARY_ITEMS = 8
INTERNAL_COMMENT_ID_RE = re.compile(
    r"\b(AI|TEST|RUN)-0*([1-9][0-9]*)\b[ \t]*",
    re.IGNORECASE,
)
PUBLIC_COMMENT_ID_TEMPLATES = {
    "AI": "问题 {number}",
    "TEST": "建议测试 {number}",
    "RUN": "相关验证",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--comment-output", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--requested-base-sha", default="")
    parser.add_argument(
        "--diff-mode",
        choices=("two-point", "merge-base"),
        default="two-point",
    )
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--local-ci-status", default="")
    parser.add_argument("--tested-sha-kind", default="commit")
    parser.add_argument("--changed-file-count", required=True, type=int)
    parser.add_argument("--changed-files-manifest", required=True)
    parser.add_argument("--repository-root", default="")
    parser.add_argument(
        "--constraint-status",
        choices=("pass", "warning", "not_applicable"),
        default="pass",
    )
    parser.add_argument(
        "--constraint-reason",
        default="未发现测试数量或耗时超出轻量约束。",
    )
    return parser.parse_args()


def require_exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{location} keys mismatch; missing={missing}, extra={extra}")


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value.strip()


def require_chinese_string(value: Any, location: str) -> str:
    text = require_string(value, location)
    if not CHINESE_TEXT_RE.search(text):
        raise ValueError(f"{location} must contain Chinese explanatory text")
    return text


def assessment_evidence_items(value: Any, location: str) -> list[str]:
    if isinstance(value, str):
        return [require_chinese_string(value, location)]
    if not isinstance(value, list) or not value:
        raise ValueError(f"{location} must be a non-empty string or array")
    if len(value) > MAX_ASSESSMENT_EVIDENCE_ITEMS:
        raise ValueError(
            f"{location} must contain at most {MAX_ASSESSMENT_EVIDENCE_ITEMS} items"
        )
    items = [
        require_chinese_string(item, f"{location}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(items)) != len(items):
        raise ValueError(f"{location} must not contain duplicate items")
    return items


def test_execution_summary_items(value: Any, location: str) -> list[str]:
    if isinstance(value, str):
        return [require_chinese_string(value, location)]
    if not isinstance(value, list) or not value:
        raise ValueError(f"{location} must be a non-empty string or array")
    if len(value) > MAX_TEST_EXECUTION_SUMMARY_ITEMS:
        raise ValueError(
            f"{location} must contain at most "
            f"{MAX_TEST_EXECUTION_SUMMARY_ITEMS} items"
        )
    items = [
        require_chinese_string(item, f"{location}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(items)) != len(items):
        raise ValueError(f"{location} must not contain duplicate items")
    return items


def public_comment_identifier(
    match: re.Match[str], identifier_descriptions: dict[str, str]
) -> str:
    number = int(match.group(2))
    identifier = f"{match.group(1).upper()}-{number:03d}"
    if identifier in identifier_descriptions:
        return identifier_descriptions[identifier]
    template = PUBLIC_COMMENT_ID_TEMPLATES[match.group(1).upper()]
    return template.format(number=number)


def validate_changed_files_manifest(document: Any) -> list[dict[str, str]]:
    if not isinstance(document, list):
        raise ValueError("changed files manifest must be an array")
    manifest: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(document):
        location = f"changed_files_manifest[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{location} must be an object")
        change_type = require_string(item.get("change_type"), f"{location}.change_type")
        expected_keys = (
            MANIFEST_RENAME_KEYS if change_type == "renamed" else MANIFEST_FILE_KEYS
        )
        require_exact_keys(item, expected_keys, location)
        if change_type not in CHANGE_TYPES:
            raise ValueError(f"{location}.change_type is invalid")
        path = require_string(item["path"], f"{location}.path")
        if path in seen_paths:
            raise ValueError(f"duplicate manifest path: {path}")
        seen_paths.add(path)
        normalized = {"path": path, "change_type": change_type}
        if change_type == "renamed":
            normalized["previous_path"] = require_string(
                item["previous_path"], f"{location}.previous_path"
            )
        manifest.append(normalized)
    return manifest


def parse_finding_line_range(value: Any, location: str) -> tuple[int, int]:
    text = require_string(value, location)
    line_range = parse_shared_finding_line_range(text)
    if line_range is None:
        raise ValueError(
            f"{location} must be a positive line number or a line range of at most "
            f"{MAX_FINDING_LINE_SPAN} lines"
        )
    return line_range


def validate_finding_location(
    finding_file: str,
    line_range: tuple[int, int],
    expected_files: list[dict[str, str]],
    repository_root: Path | None,
    location: str,
) -> None:
    relative_path = normalized_repository_path(finding_file)
    if relative_path is None:
        raise ValueError(
            f"{location}.file must be a normalized repository-relative path"
        )

    change_types = {item["path"]: item["change_type"] for item in expected_files}
    change_type = change_types.get(finding_file)
    if change_type is None:
        raise ValueError(f"{location}.file must be a changed file in the Git diff")
    if change_type == "deleted":
        raise ValueError(
            f"{location}.file is deleted; anchor the finding to a retained changed call site"
        )

    if repository_root is None:
        return
    source_path = repository_root.joinpath(*relative_path.parts)
    try:
        resolved_source = source_path.resolve(strict=True)
        resolved_source.relative_to(repository_root)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{location}.file is not a readable file in the review checkout"
        ) from exc
    if not resolved_source.is_file():
        raise ValueError(
            f"{location}.file is not a regular file in the review checkout"
        )
    try:
        source_lines = resolved_source.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as exc:
        raise ValueError(
            f"{location}.file could not be read from the review checkout"
        ) from exc
    start, end = line_range
    if end > len(source_lines):
        raise ValueError(
            f"{location}.line {start}-{end} is outside {finding_file} with "
            f"{len(source_lines)} lines"
        )
    relevant_lines = source_lines[start - 1 : end]
    if not any(line.strip() for line in relevant_lines):
        raise ValueError(f"{location}.line points only to blank lines")


def validate_report(
    document: Any,
    expected_files: list[dict[str, str]],
    repository_root: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("report root must be an object")
    if repository_root is not None:
        repository_root = repository_root.resolve()
    require_exact_keys(document, ROOT_KEYS, "report")

    verdict = require_string(document["verdict"], "verdict")
    if verdict not in {"PASS", "WARNING", "FAIL"}:
        raise ValueError(f"unsupported verdict: {verdict}")
    require_chinese_string(document["summary"], "summary")
    require_chinese_string(document["merge_recommendation"], "merge_recommendation")
    assessment = document["change_request_assessment"]
    if not isinstance(assessment, dict):
        raise ValueError("change_request_assessment must be an object")
    require_exact_keys(
        assessment,
        CHANGE_REQUEST_ASSESSMENT_KEYS,
        "change_request_assessment",
    )
    assessment_status = require_string(
        assessment["status"], "change_request_assessment.status"
    )
    if assessment_status not in CHANGE_REQUEST_ASSESSMENT_STATUSES:
        raise ValueError("change_request_assessment.status is invalid")
    for key in {
        "contributor_goal",
        "expected_behavior",
        "implementation_summary",
    }:
        require_chinese_string(assessment[key], f"change_request_assessment.{key}")
    assessment_evidence_items(
        assessment["evidence"], "change_request_assessment.evidence"
    )
    if document["completion_marker"] != "CODEX_AI_CI_COMPLETE":
        raise ValueError("completion_marker is invalid")

    changed_files = document["changed_files"]
    if not isinstance(changed_files, list):
        raise ValueError("changed_files must be an array")
    actual_files: dict[str, str] = {}
    for index, changed_file in enumerate(changed_files):
        location = f"changed_files[{index}]"
        if not isinstance(changed_file, dict):
            raise ValueError(f"{location} must be an object")
        require_exact_keys(changed_file, CHANGED_FILE_KEYS, location)
        path = require_string(changed_file["path"], f"{location}.path")
        change_type = require_string(
            changed_file["change_type"], f"{location}.change_type"
        )
        if change_type not in CHANGE_TYPES:
            raise ValueError(f"{location}.change_type is invalid")
        if path in actual_files:
            raise ValueError(f"duplicate changed_files path: {path}")
        actual_files[path] = change_type
        for key in {"summary", "impact", "validation_strategy"}:
            require_chinese_string(changed_file[key], f"{location}.{key}")

    expected_map = {
        item["path"]: item["change_type"]
        for item in expected_files
    }
    if actual_files != expected_map:
        missing = sorted(set(expected_map) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected_map))
        mismatched = sorted(
            path
            for path in set(actual_files) & set(expected_map)
            if actual_files[path] != expected_map[path]
        )
        raise ValueError(
            "changed_files does not match Git diff manifest; "
            f"missing={missing}, extra={extra}, mismatched={mismatched}"
        )

    behavior_coverage = document["behavior_coverage"]
    if not isinstance(behavior_coverage, dict):
        raise ValueError("behavior_coverage must be an object")
    require_exact_keys(
        behavior_coverage, BEHAVIOR_COVERAGE_KEYS, "behavior_coverage"
    )
    for behavior_name in BEHAVIOR_COVERAGE_KEYS:
        behavior = behavior_coverage[behavior_name]
        location = f"behavior_coverage.{behavior_name}"
        if not isinstance(behavior, dict):
            raise ValueError(f"{location} must be an object")
        require_exact_keys(behavior, BEHAVIOR_ITEM_KEYS, location)
        for key in BEHAVIOR_ITEM_KEYS:
            require_chinese_string(behavior[key], f"{location}.{key}")

    findings = document["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings must be an array")
    finding_ids: set[str] = set()
    for index, finding in enumerate(findings):
        location = f"findings[{index}]"
        if not isinstance(finding, dict):
            raise ValueError(f"{location} must be an object")
        require_exact_keys(finding, FINDING_KEYS, location)
        finding_id = require_string(finding["id"], f"{location}.id")
        if not re.fullmatch(r"AI-[0-9]{3}", finding_id):
            raise ValueError(f"{location}.id has an invalid format")
        if finding_id in finding_ids:
            raise ValueError(f"duplicate finding id: {finding_id}")
        finding_ids.add(finding_id)
        severity = require_string(finding["severity"], f"{location}.severity")
        category = require_string(finding["category"], f"{location}.category")
        if severity not in SEVERITIES:
            raise ValueError(f"{location}.severity is invalid")
        if category not in CATEGORIES:
            raise ValueError(f"{location}.category is invalid")
        finding_file = require_string(finding["file"], f"{location}.file")
        line_range = parse_finding_line_range(finding["line"], f"{location}.line")
        validate_finding_location(
            finding_file,
            line_range,
            expected_files,
            repository_root,
            location,
        )
        for key in {"code_role", "title", "evidence", "impact", "fix_direction"}:
            require_chinese_string(finding[key], f"{location}.{key}")

    tests = document["suggested_tests"]
    if not isinstance(tests, list):
        raise ValueError("suggested_tests must be an array")
    test_ids: set[str] = set()
    for index, test in enumerate(tests):
        location = f"suggested_tests[{index}]"
        if not isinstance(test, dict):
            raise ValueError(f"{location} must be an object")
        require_exact_keys(test, TEST_KEYS, location)
        test_id = require_string(test["id"], f"{location}.id")
        if not re.fullmatch(r"TEST-[0-9]{3}", test_id):
            raise ValueError(f"{location}.id has an invalid format")
        if test_id in test_ids:
            raise ValueError(f"duplicate test id: {test_id}")
        test_ids.add(test_id)
        priority = require_string(test["priority"], f"{location}.priority")
        if priority not in SEVERITIES:
            raise ValueError(f"{location}.priority is invalid")
        require_string(test["target"], f"{location}.target")
        require_chinese_string(test["description"], f"{location}.description")

    residual_risks = document["residual_risks"]
    if not isinstance(residual_risks, list):
        raise ValueError("residual_risks must be an array")
    for index, risk in enumerate(residual_risks):
        require_chinese_string(risk, f"residual_risks[{index}]")

    test_execution = document["test_execution"]
    if not isinstance(test_execution, dict):
        raise ValueError("test_execution must be an object")
    require_exact_keys(test_execution, TEST_EXECUTION_KEYS, "test_execution")
    execution_status = require_string(
        test_execution["status"], "test_execution.status"
    )
    if execution_status not in TEST_EXECUTION_STATUSES:
        raise ValueError(f"unsupported test execution status: {execution_status}")
    test_execution_summary_items(
        test_execution["summary"], "test_execution.summary"
    )

    generated_test_files = test_execution["generated_test_files"]
    if not isinstance(generated_test_files, list):
        raise ValueError("test_execution.generated_test_files must be an array")
    for index, file_name in enumerate(generated_test_files):
        require_string(file_name, f"test_execution.generated_test_files[{index}]")

    commands = test_execution["commands"]
    if not isinstance(commands, list):
        raise ValueError("test_execution.commands must be an array")
    command_ids: set[str] = set()
    command_statuses: list[str] = []
    command_statuses_by_text: dict[str, list[str]] = {}
    for index, command in enumerate(commands):
        location = f"test_execution.commands[{index}]"
        if not isinstance(command, dict):
            raise ValueError(f"{location} must be an object")
        require_exact_keys(command, COMMAND_KEYS, location)
        command_id = require_string(command["id"], f"{location}.id")
        if not re.fullmatch(r"RUN-[0-9]{3}", command_id):
            raise ValueError(f"{location}.id has an invalid format")
        if command_id in command_ids:
            raise ValueError(f"duplicate command id: {command_id}")
        command_ids.add(command_id)
        command_text = require_string(command["command"], f"{location}.command")
        purpose = require_chinese_string(command["purpose"], f"{location}.purpose")
        if len(purpose) > 120:
            raise ValueError(
                f"{location}.purpose must contain at most 120 characters"
            )
        if INTERNAL_COMMENT_ID_RE.search(purpose):
            raise ValueError(f"{location}.purpose must not contain an internal ID")
        if not isinstance(command["exit_code"], int):
            raise ValueError(f"{location}.exit_code must be an integer")
        duration = command["duration_seconds"]
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            raise ValueError(f"{location}.duration_seconds must be a number")
        if duration < 0:
            raise ValueError(f"{location}.duration_seconds must not be negative")
        command_status = require_string(command["status"], f"{location}.status")
        if command_status not in COMMAND_STATUSES:
            raise ValueError(f"{location}.status is invalid")
        if command_status == "passed" and command["exit_code"] != 0:
            raise ValueError(f"{location}.passed command must have exit_code 0")
        if command_status in {
            "stable_failure",
            "flaky_failure",
            "infrastructure_failure",
        } and command["exit_code"] == 0:
            raise ValueError(
                f"{location}.{command_status} command must have a non-zero exit_code"
            )
        command_statuses.append(command_status)
        command_statuses_by_text.setdefault(command_text, []).append(command_status)
        require_chinese_string(command["evidence"], f"{location}.evidence")

    executed_statuses = [
        status for status in command_statuses if status != "not_executed"
    ]
    if execution_status == "passed":
        if not executed_statuses or any(
            status != "passed" for status in executed_statuses
        ):
            raise ValueError(
                "test_execution.status passed requires at least one executed command "
                "and all executed commands to pass"
            )
    elif execution_status == "not_run" and executed_statuses:
        raise ValueError("test_execution.status not_run cannot contain executed commands")
    elif execution_status == "stable_failure":
        has_comparable_repeat = any(
            statuses.count("stable_failure") >= 2
            for statuses in command_statuses_by_text.values()
        )
        if not has_comparable_repeat or any(
            status not in {"passed", "stable_failure"}
            for status in executed_statuses
        ):
            raise ValueError(
                "test_execution.status stable_failure requires at least two "
                "stable_failure command records"
            )
    elif execution_status == "flaky_failure":
        has_inconsistent_repeat = any(
            "passed" in statuses and "flaky_failure" in statuses
            for statuses in command_statuses_by_text.values()
        )
        if not has_inconsistent_repeat or any(
            status not in {"passed", "flaky_failure"}
            for status in executed_statuses
        ):
            raise ValueError(
                "test_execution.status flaky_failure requires both passed and "
                "flaky_failure command records"
            )
    elif execution_status == "infrastructure_failure":
        if "infrastructure_failure" not in command_statuses or any(
            status not in {"passed", "infrastructure_failure"}
            for status in executed_statuses
        ):
            raise ValueError(
                "test_execution.status infrastructure_failure requires an "
                "infrastructure_failure command record"
            )

    warning_execution_statuses = {
        "stable_failure",
        "flaky_failure",
        "infrastructure_failure",
        "test_generation_error",
        "insufficient_evidence",
    }
    expected_verdict = (
        "FAIL"
        if any(finding["severity"] == "HIGH" for finding in findings)
        else "WARNING"
        if findings or execution_status in warning_execution_statuses
        else "PASS"
    )
    if verdict != expected_verdict:
        raise ValueError(
            f"verdict {verdict} does not match findings and test_execution.status; "
            f"expected {expected_verdict}"
        )
    return document


def inline(value: Any) -> str:
    text = " ".join(str(value).split())
    return html.escape(text, quote=False).replace("|", "\\|").replace("`", "'")


CATEGORY_LABELS = {
    "algorithm": "算法错误",
    "business-logic": "业务逻辑错误",
    "state-management": "状态错误",
    "cache-consistency": "缓存一致性错误",
    "concurrency": "并发错误",
    "resource-lifecycle": "资源生命周期错误",
    "data-integrity": "数据完整性问题",
    "correctness": "正确性错误",
    "regression": "行为回归",
    "security": "安全问题",
    "api-compatibility": "接口兼容性问题",
    "performance": "性能问题",
    "test-gap": "测试缺口",
    "other": "其他问题",
}
SEVERITY_LABELS = {
    "HIGH": "高风险",
    "MEDIUM": "中风险",
    "LOW": "低风险",
}
VERDICT_LABELS = {
    "PASS": "通过",
    "WARNING": "警告",
    "FAIL": "失败",
}
TEST_EXECUTION_STATUS_LABELS = {
    "not_run": "未执行",
    "passed": "所执行的验证命令均通过",
    "stable_failure": "可稳定复现的失败",
    "flaky_failure": "非确定性失败",
    "infrastructure_failure": "基础设施失败",
    "test_generation_error": "测试生成失败",
    "insufficient_evidence": "证据不足",
}
COMMAND_STATUS_LABELS = {
    "passed": "通过",
    "stable_failure": "可稳定复现的失败",
    "flaky_failure": "非确定性失败",
    "infrastructure_failure": "基础设施失败",
    "not_executed": "未执行",
}
CONSTRAINT_STATUS_LABELS = {
    "pass": "通过",
    "warning": "警告",
    "not_applicable": "不适用",
}
CHANGE_TYPE_LABELS = {
    "modified": "修改",
    "added": "新增",
    "deleted": "删除",
    "renamed": "重命名",
}
CHANGE_REQUEST_ASSESSMENT_LABELS = {
    "implemented": "已实现",
    "partially_implemented": "部分实现",
    "not_implemented": "未实现",
    "not_assessable": "无法判断",
    "not_applicable": "不适用",
}
BEHAVIOR_LABELS = {
    "normal": "正常路径",
    "boundary": "边界路径",
    "error": "错误路径",
    "compatibility": "兼容路径",
    "integration": "集成路径",
}
BEHAVIOR_ORDER = ("normal", "boundary", "error", "compatibility", "integration")
MAX_COMMENT_LENGTH = 58_000


def comment_inline(value: Any, limit: int = 2_000) -> str:
    text = inline(value).replace("@", "＠")
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 1, 0)]}…"


def public_comment_text(text: str, identifier_descriptions: dict[str, str]) -> str:
    return INTERNAL_COMMENT_ID_RE.sub(
        lambda match: public_comment_identifier(match, identifier_descriptions),
        text,
    )


def render_report(document: dict[str, Any], args: argparse.Namespace) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assessment = document["change_request_assessment"]
    assessment_evidence = assessment_evidence_items(
        assessment["evidence"], "change_request_assessment.evidence"
    )
    if args.diff_mode == "merge-base":
        base_rows = [
            f"| 目标分支提交 | `{inline(args.requested_base_sha)}` |",
            f"| 实际审查起点（merge-base） | `{inline(args.base_sha)}` |",
        ]
    else:
        base_rows = [
            f"| 基础提交 | `{inline(args.base_sha)}` |",
        ]
    lines = [
        "# Codex AI 自动审查报告",
        "",
        "## 元数据",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        "| 报告格式 | `triton-anchor-codex-ai-report/v3` |",
        f"| 分支 | `{inline(args.branch)}` |",
        *base_rows,
        f"| 测试提交 | `{inline(args.target_sha)}` |",
        f"| 测试提交类型 | `{inline(args.tested_sha_kind)}` |",
        *([f"| PR Head 提交 | `{inline(args.head_sha)}` |"] if args.head_sha else []),
        f"| 变更文件数 | {args.changed_file_count} |",
        f"| 生成时间（UTC） | `{generated_at}` |",
        "",
        "## 结论",
        "",
        f"**{VERDICT_LABELS[document['verdict']]}**",
        "",
        "## 摘要",
        "",
        inline(document["summary"]),
        "",
        "## 贡献者目标与实现情况",
        "",
        f"- 判断：{CHANGE_REQUEST_ASSESSMENT_LABELS[assessment['status']]}",
        f"- 修改目标：{inline(assessment['contributor_goal'])}",
        f"- 预期行为：{inline(assessment['expected_behavior'])}",
        f"- 实现情况：{inline(assessment['implementation_summary'])}",
        "- 判断依据：",
        *[f"  - {inline(item)}" for item in assessment_evidence],
        "",
        "## 合入建议",
        "",
        inline(document["merge_recommendation"]),
        "",
        "## 具体文件变更",
        "",
        "| 文件 | 类型 | 改动说明 | 影响 | 已执行验证或未执行原因 |",
        "| --- | --- | --- | --- | --- |",
    ]

    for changed_file in document["changed_files"]:
        lines.append(
            f"| `{inline(changed_file['path'])}` | "
            f"{CHANGE_TYPE_LABELS[changed_file['change_type']]} | "
            f"{inline(changed_file['summary'])} | "
            f"{inline(changed_file['impact'])} | "
            f"{inline(changed_file['validation_strategy'])} |"
        )
    if not document["changed_files"]:
        lines.append("| 无 | 无 | 本次差异没有变更文件。 | 不适用。 | 不适用。 |")

    lines.extend([
        "",
        "## 行为覆盖",
        "",
        "| 路径 | 检查范围 | 验证策略 | 结果 |",
        "| --- | --- | --- | --- |",
    ])
    for behavior_name in BEHAVIOR_ORDER:
        behavior = document["behavior_coverage"][behavior_name]
        lines.append(
            f"| {BEHAVIOR_LABELS[behavior_name]} | "
            f"{inline(behavior['scope'])} | "
            f"{inline(behavior['strategy'])} | "
            f"{inline(behavior['result'])} |"
        )
    lines.extend(["", "## 关键问题", ""])

    findings = document["findings"]
    if not findings:
        lines.extend(["未发现需要阻塞合并的关键问题。", ""])
    else:
        for finding in findings:
            location = f"{inline(finding['file'])}:{inline(finding['line'])}"
            lines.extend([
                f"### {finding['id']}: {inline(finding['title'])}",
                "",
                "| 字段 | 值 |",
                "| --- | --- |",
                f"| 风险级别 | **{SEVERITY_LABELS[finding['severity']]}** |",
                f"| 类别 | {CATEGORY_LABELS[finding['category']]} |",
                f"| 位置 | `{location}` |",
                f"| 这段代码负责 | {inline(finding['code_role'])} |",
                f"| 证据 | {inline(finding['evidence'])} |",
                f"| 影响 | {inline(finding['impact'])} |",
                f"| 修复方向 | {inline(finding['fix_direction'])} |",
                "",
            ])

    lines.extend(["## 建议测试", ""])
    tests = document["suggested_tests"]
    if not tests:
        lines.extend(["无。", ""])
    else:
        lines.extend([
            "| 编号 | 优先级 | 目标 | 说明 |",
            "| --- | --- | --- | --- |",
        ])
        for test in tests:
            lines.append(
                f"| {test['id']} | **{test['priority']}** | "
                f"`{inline(test['target'])}` | {inline(test['description'])} |"
            )
        lines.append("")

    test_execution = document["test_execution"]
    test_execution_summary = test_execution_summary_items(
        test_execution["summary"], "test_execution.summary"
    )
    lines.extend([
        "## 测试执行",
        "",
        f"- 状态：{TEST_EXECUTION_STATUS_LABELS[test_execution['status']]}",
        "- 说明：",
        *[f"  - {inline(item)}" for item in test_execution_summary],
        "",
        "### 生成的测试文件",
        "",
    ])
    generated_test_files = test_execution["generated_test_files"]
    if generated_test_files:
        lines.extend(f"- `{inline(file_name)}`" for file_name in generated_test_files)
    else:
        lines.append("无。")
    lines.extend(["", "### 执行命令", ""])
    commands = test_execution["commands"]
    if commands:
        lines.extend([
            "| 编号 | 功能 | 状态 | 退出码 | 耗时（秒） | 命令 | 证据 |",
            "| --- | --- | --- | ---: | ---: | --- | --- |",
        ])
        for command in commands:
            lines.append(
                f"| {command['id']} | {inline(command['purpose'])} | "
                f"{COMMAND_STATUS_LABELS[command['status']]} | "
                f"{command['exit_code']} | {command['duration_seconds']} | "
                f"`{inline(command['command'])}` | {inline(command['evidence'])} |"
            )
    else:
        lines.append("未记录执行命令。")
    lines.extend([
        "",
        "## 测试执行约束",
        "",
        f"- 状态：{CONSTRAINT_STATUS_LABELS[args.constraint_status]}",
        f"- 说明：{inline(args.constraint_reason)}",
        "",
        "## 剩余风险",
        "",
    ])
    risks = document["residual_risks"]
    if risks:
        lines.extend(f"- {inline(risk)}" for risk in risks)
    else:
        lines.append("未报告剩余风险。")
    lines.extend(["", "## 执行标记", "", "CODEX_AI_CI_COMPLETE", ""])
    return "\n".join(lines)


def deterministic_ci_comment_line(args: argparse.Namespace) -> str:
    status = getattr(args, "local_ci_status", "")
    if status in {0, "0"}:
        return "已通过；Codex AI 自动审查只提供补充意见，不改变门禁结果。"
    if status:
        return "未通过；Codex AI 自动审查用于辅助定位原因，最终仍以检查结果和复测为准。"
    return "结果尚未提供；Codex AI 自动审查只提供补充意见。"


def render_comment(document: dict[str, Any], args: argparse.Namespace) -> str:
    findings = sorted(
        document["findings"],
        key=lambda finding: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[
            finding["severity"]
        ],
    )
    test_execution = document["test_execution"]
    test_execution_summary = test_execution_summary_items(
        test_execution["summary"], "test_execution.summary"
    )
    identifier_descriptions = {
        command["id"]: command["purpose"]
        for command in test_execution["commands"]
    }
    assessment = document["change_request_assessment"]
    assessment_evidence = assessment_evidence_items(
        assessment["evidence"], "change_request_assessment.evidence"
    )
    lines = [
        "## Codex AI 自动审查",
        "",
        "> Codex AI 自动审查仅供参考且不阻塞合入；本地确定性 CI 检查结果才是合入门禁。",
        "",
        "### 审查摘要",
        "",
        f"- Codex AI 审查结论：**{VERDICT_LABELS[document['verdict']]}**",
        f"- 本地确定性 CI 检查：{deterministic_ci_comment_line(args)}",
        f"- 合入建议：{comment_inline(document['merge_recommendation'], 1_000)}",
        "",
        comment_inline(document["summary"]),
        "",
        "### 贡献者目标与实现情况",
        "",
        f"- 判断：**{CHANGE_REQUEST_ASSESSMENT_LABELS[assessment['status']]}**",
        f"- 贡献者目标：{comment_inline(assessment['contributor_goal'], 1_500)}",
        f"- 预期效果：{comment_inline(assessment['expected_behavior'], 1_500)}",
        f"- 当前实现情况：{comment_inline(assessment['implementation_summary'], 2_000)}",
        "- 判断依据：",
        *[f"  - {comment_inline(item, 1_500)}" for item in assessment_evidence],
        "",
        "### 需要处理的问题",
        "",
    ]
    if not findings:
        lines.extend([
            "基于当前代码差异和验证证据，没有发现需要处理的具体缺陷。",
            "",
        ])
    else:
        shown = findings[:5]
        for index, finding in enumerate(shown, start=1):
            category = CATEGORY_LABELS.get(finding["category"], "其他问题")
            severity = SEVERITY_LABELS[finding["severity"]]
            location = (
                f"{comment_inline(finding['file'])}:"
                f"{comment_inline(finding['line'])}"
            )
            lines.append(
                f"#### {index}. [{severity}] {comment_inline(finding['title'], 400)}"
            )
            lines.append("")
            lines.append(f"- 问题类型：{category}")
            lines.append(f"- 代码定位：`{location}`")
            lines.append(f"- 这段代码负责：{comment_inline(finding['code_role'], 1_000)}")
            lines.append(f"- 影响：{comment_inline(finding['impact'], 1_000)}")
            lines.append(f"- 建议：{comment_inline(finding['fix_direction'], 1_000)}")
            lines.append("")
        if len(findings) > len(shown):
            lines.extend([
                f"另有 {len(findings) - len(shown)} 个问题，请查看完整报告。",
                "",
            ])

    lines.extend([
        "### 验证情况",
        "",
        f"- 补充验证结果：**{TEST_EXECUTION_STATUS_LABELS[test_execution['status']]}**",
        "- 说明：",
        *[f"  - {comment_inline(item, 1_500)}" for item in test_execution_summary],
        "",
    ])
    if args.constraint_status == "warning":
        lines.extend([
            f"- 验证范围提醒：{comment_inline(args.constraint_reason, 1_500)}",
            "",
        ])

    lines.extend([
        "### 变更文件",
        "",
        "<details>",
        "<summary>查看变更文件</summary>",
        "",
        "| 文件 | 类型 | 改动说明 | 影响 |",
        "| --- | --- | --- | --- |",
    ])
    table_suffix = ["", "</details>", ""]
    changed_files = document["changed_files"]
    if not changed_files:
        lines.append("| 无 | 无 | 本次差异没有变更文件。 | 不适用。 |")
    else:
        for index, changed_file in enumerate(changed_files):
            row = (
                f"| `{comment_inline(changed_file['path'], 500)}` | "
                f"{CHANGE_TYPE_LABELS[changed_file['change_type']]} | "
                f"{comment_inline(changed_file['summary'], 800)} | "
                f"{comment_inline(changed_file['impact'], 800)} |"
            )
            candidate = "\n".join([*lines, row, *table_suffix])
            if len(candidate) > MAX_COMMENT_LENGTH:
                remaining = len(changed_files) - index
                lines.append(
                    f"| 其余 {remaining} 个文件 | 省略 | "
                    "评论长度接近 GitHub 限制，请查看完整报告。 | "
                    "完整验证策略保留在完整报告中。 |"
                )
                break
            lines.append(row)
    lines.extend(table_suffix)
    return public_comment_text("\n".join(lines), identifier_descriptions)


def main() -> int:
    args = parse_args()
    try:
        input_path = Path(args.input)
        manifest_path = Path(args.changed_files_manifest)
        expected_files = validate_changed_files_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        if len(expected_files) != args.changed_file_count:
            raise ValueError(
                "changed file count does not match manifest; "
                f"count={args.changed_file_count}, manifest={len(expected_files)}"
            )
        repository_root = None
        if args.repository_root:
            repository_root = Path(args.repository_root).resolve()
            if not repository_root.is_dir():
                raise ValueError("repository_root must be an existing directory")
        document = validate_report(
            json.loads(input_path.read_text(encoding="utf-8")),
            expected_files,
            repository_root,
        )
        require_chinese_string(args.constraint_reason, "constraint_reason")
        rendered = render_report(document, args)
        output_path = Path(args.output)
        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary_path.write_text(rendered, encoding="utf-8")
        temporary_path.replace(output_path)
        comment_path = Path(args.comment_output)
        comment_temporary_path = comment_path.with_suffix(comment_path.suffix + ".tmp")
        comment_temporary_path.write_text(render_comment(document, args), encoding="utf-8")
        comment_temporary_path.replace(comment_path)
        print(document["verdict"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"Invalid Codex AI report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
