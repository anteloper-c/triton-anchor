#!/usr/bin/env python3
"""Build canonical Codex AI report v3 from a small analysis payload and trusted facts."""

from __future__ import annotations

import argparse
import json
import re
import tarfile
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any


CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
FILE_ID_RE = re.compile(r"FILE-[0-9]{3}")
LINE_RE = re.compile(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?")
SEVERITIES = {"HIGH", "MEDIUM", "LOW"}
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
ASSESSMENT_STATUSES = {
    "implemented",
    "partially_implemented",
    "not_implemented",
    "not_assessable",
    "not_applicable",
}
EVIDENCE_LEVELS = {
    "not_needed",
    "sufficient",
    "insufficient",
    "test_generation_error",
}
FAILURE_CLASSIFICATIONS = {
    "none",
    "product",
    "flaky",
    "infrastructure",
    "unknown",
}
WARNING_EXECUTION_STATUSES = {
    "stable_failure",
    "flaky_failure",
    "infrastructure_failure",
    "test_generation_error",
    "insufficient_evidence",
}
BEHAVIOR_LABELS = {
    "normal": "正常路径",
    "boundary": "边界路径",
    "error": "错误路径",
    "compatibility": "兼容路径",
    "integration": "集成路径",
}
ANALYSIS_KEYS = {
    "summary",
    "merge_recommendation",
    "change_request_assessment",
    "changed_files",
    "behavior_coverage",
    "findings",
    "suggested_tests",
    "residual_risks",
    "test_assessment",
}
ASSESSMENT_KEYS = {
    "status",
    "contributor_goal",
    "expected_behavior",
    "implementation_summary",
    "evidence",
}
CHANGED_FILE_KEYS = {"file_id", "summary", "impact", "validation_strategy"}
BEHAVIOR_ITEM_KEYS = {"scope", "strategy", "result"}
FINDING_KEYS = {
    "severity",
    "category",
    "file_id",
    "line",
    "code_role",
    "title",
    "evidence",
    "impact",
    "fix_direction",
}
SUGGESTED_TEST_KEYS = {"priority", "target", "description"}
TEST_ASSESSMENT_KEYS = {"evidence_level", "summary", "commands"}
COMMAND_ANNOTATION_KEYS = {
    "command",
    "purpose",
    "evidence",
    "failure_classification",
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    return value


def require_array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array")
    return value


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    return value


def require_exact_keys(
    document: dict[str, Any], expected: set[str], location: str
) -> None:
    actual = set(document)
    if actual != expected:
        raise ValueError(
            f"{location} has invalid keys; "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}"
        )


def text_or_default(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    text = value.strip()
    if CHINESE_RE.search(text) is None:
        return f"Codex 原始说明：{text}"
    return text


def normalized_repo_path(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{location} must be a normalized repository-relative path")
    return value


def load_manifest(path: Path) -> list[dict[str, str]]:
    document = load_json(path)
    if not isinstance(document, list):
        raise ValueError("changed_files_manifest must be an array")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(document):
        location = f"changed_files_manifest[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{location} must be an object")
        change_type = raw.get("change_type")
        if change_type not in {"added", "modified", "deleted", "renamed"}:
            raise ValueError(f"{location}.change_type is invalid")
        expected = (
            {"path", "change_type", "previous_path"}
            if change_type == "renamed"
            else {"path", "change_type"}
        )
        if set(raw) != expected:
            raise ValueError(f"{location} has invalid keys")
        item = {
            "path": normalized_repo_path(raw["path"], f"{location}.path"),
            "change_type": change_type,
        }
        if item["path"] in seen:
            raise ValueError(f"duplicate manifest path: {item['path']}")
        seen.add(item["path"])
        if change_type == "renamed":
            item["previous_path"] = normalized_repo_path(
                raw["previous_path"], f"{location}.previous_path"
            )
        result.append(item)
    return result


def prepare_manifest(input_path: Path, output_path: Path) -> None:
    write_json(
        output_path,
        [
            {"file_id": f"FILE-{index:03d}", **item}
            for index, item in enumerate(load_manifest(input_path), start=1)
        ],
    )


def parse_generated_archive(path: Path) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    generated: list[str] = []
    with tarfile.open(path, mode="r:gz") as archive:
        for index, member in enumerate(archive.getmembers()):
            location = f"generated archive member[{index}]"
            relative = normalized_repo_path(member.name, location)
            if not member.isfile():
                raise ValueError(f"{location} must be a regular file: {relative}")
            if relative in generated:
                raise ValueError(f"duplicate generated archive path: {relative}")
            generated.append(relative)
    return generated


def is_test_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    filename = path.name.lower()
    directory_parts = {part.lower() for part in path.parts[:-1]}
    return (
        bool(directory_parts & {"test", "tests", "generated_tests"})
        or filename.startswith("test_")
        or filename.startswith("test-")
        or re.search(r"(?:^|[_-])tests?(?:[._-]|$)", filename) is not None
    )


def load_command_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    document = load_json(path)
    if not isinstance(document, list):
        raise ValueError("command_ledger must be an array")
    ledger: list[dict[str, Any]] = []
    for index, raw in enumerate(document):
        location = f"command_ledger[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{location} must be an object")
        command = raw.get("command")
        exit_code = raw.get("exit_code")
        duration = raw.get("duration_seconds", 0.0)
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{location}.command must be a non-empty string")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(f"{location}.exit_code must be an integer")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration < 0
        ):
            raise ValueError(f"{location}.duration_seconds must be non-negative")
        ledger.append(
            {
                "command": command.strip(),
                "exit_code": exit_code,
                "duration_seconds": round(float(duration), 3),
            }
        )
    return ledger


def validate_line(value: Any, location: str, file_path: str, root: Path) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a line number or range")
    match = LINE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{location} must be a positive line or line range")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start or end - start + 1 > 12:
        raise ValueError(f"{location} must span at most 12 lines")
    repository_root = root.resolve(strict=True)
    candidate = repository_root.joinpath(*PurePosixPath(file_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{location} references an unreadable changed file") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise ValueError(f"{location} references a non-regular changed file")
    line_count = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
    if end > line_count:
        raise ValueError(f"{location} exceeds the changed file line count")
    return value


def semantic_command_annotations(
    analysis: dict[str, Any]
) -> dict[str, deque[dict[str, Any]]]:
    assessment = require_object(analysis["test_assessment"], "test_assessment")
    result: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for index, value in enumerate(
        require_array(assessment["commands"], "test_assessment.commands")
    ):
        location = f"test_assessment.commands[{index}]"
        raw = require_object(value, location)
        require_exact_keys(raw, COMMAND_ANNOTATION_KEYS, location)
        command = require_string(raw["command"], f"{location}.command")
        purpose = require_string(raw["purpose"], f"{location}.purpose")
        evidence = require_string(raw["evidence"], f"{location}.evidence")
        classification = require_string(
            raw["failure_classification"], f"{location}.failure_classification"
        )
        if classification not in FAILURE_CLASSIFICATIONS:
            raise ValueError(f"{location}.failure_classification is invalid")
        if len(purpose) > 120:
            raise ValueError(f"{location}.purpose must contain at most 120 characters")
        if not command.strip():
            continue
        result[command.strip()].append(
            {
                "purpose": text_or_default(
                    purpose, "Codex 执行的验证或诊断命令"
                ),
                "evidence": text_or_default(
                    evidence, "执行结果来自可信 Codex JSONL 事件。"
                ),
                "failure_classification": classification,
            }
        )
    return result


def build_commands(
    analysis: dict[str, Any], ledger: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], int]:
    annotations = semantic_command_annotations(analysis)
    commands: list[dict[str, Any]] = []
    classifications: list[str] = []
    annotated_count = 0
    for index, fact in enumerate(ledger, start=1):
        if annotations[fact["command"]]:
            annotation = annotations[fact["command"]].popleft()
            annotated_count += 1
        else:
            annotation = {
                "purpose": "Codex 执行的验证或诊断命令",
                "evidence": "执行结果来自可信 Codex JSONL 事件。",
                "failure_classification": (
                    "none" if fact["exit_code"] == 0 else "unknown"
                ),
            }
        classification = annotation["failure_classification"]
        if fact["exit_code"] == 0:
            classification = "none"
        classifications.append(classification)
        commands.append(
            {
                "id": f"RUN-{index:03d}",
                "purpose": annotation["purpose"],
                "command": fact["command"],
                "exit_code": fact["exit_code"],
                "duration_seconds": fact["duration_seconds"],
                "status": "passed" if fact["exit_code"] == 0 else "failed",
                "evidence": annotation["evidence"],
            }
        )

    failed_indexes = [
        index for index, command in enumerate(commands) if command["exit_code"] != 0
    ]
    if not failed_indexes:
        return commands, classifications, annotated_count
    if all(classifications[index] == "infrastructure" for index in failed_indexes):
        for index in failed_indexes:
            commands[index]["status"] = "infrastructure_failure"
        return commands, classifications, annotated_count

    outcomes: dict[str, list[int]] = defaultdict(list)
    for command in commands:
        outcomes[command["command"]].append(command["exit_code"])
    flaky = {
        text
        for text, exits in outcomes.items()
        if any(code == 0 for code in exits) and any(code != 0 for code in exits)
    }
    if flaky and all(commands[index]["command"] in flaky for index in failed_indexes):
        for index in failed_indexes:
            commands[index]["status"] = "flaky_failure"
        return commands, classifications, annotated_count

    failures = Counter(commands[index]["command"] for index in failed_indexes)
    stable = {text for text, count in failures.items() if count >= 2}
    if (
        stable
        and all(commands[index]["command"] in stable for index in failed_indexes)
        and all(classifications[index] == "product" for index in failed_indexes)
    ):
        for index in failed_indexes:
            commands[index]["status"] = "stable_failure"
    return commands, classifications, annotated_count


def derive_execution_status(
    evidence_level: str,
    commands: list[dict[str, Any]],
    *,
    annotated_command_count: int,
    has_suggested_tests: bool,
) -> str:
    if evidence_level == "test_generation_error":
        return "test_generation_error"
    if not commands:
        return "not_run" if evidence_level == "not_needed" else "insufficient_evidence"
    failed = [command for command in commands if command["exit_code"] != 0]
    if failed:
        statuses = {command["status"] for command in failed}
        if statuses == {"infrastructure_failure"}:
            return "infrastructure_failure"
        if statuses == {"flaky_failure"}:
            return "flaky_failure"
        if statuses == {"stable_failure"}:
            return "stable_failure"
        return "insufficient_evidence"
    if evidence_level in {"sufficient", "not_needed"}:
        return "passed"
    if annotated_command_count > 0 and not has_suggested_tests:
        return "passed"
    return "insufficient_evidence"


def normalize_assessment(analysis: dict[str, Any]) -> dict[str, Any]:
    raw = require_object(
        analysis["change_request_assessment"], "change_request_assessment"
    )
    require_exact_keys(raw, ASSESSMENT_KEYS, "change_request_assessment")
    status = require_string(raw["status"], "change_request_assessment.status")
    if status not in ASSESSMENT_STATUSES:
        raise ValueError("change_request_assessment.status is invalid")
    contributor_goal = require_string(
        raw["contributor_goal"], "change_request_assessment.contributor_goal"
    )
    expected_behavior = require_string(
        raw["expected_behavior"], "change_request_assessment.expected_behavior"
    )
    implementation_summary = require_string(
        raw["implementation_summary"],
        "change_request_assessment.implementation_summary",
    )
    raw_evidence = require_array(
        raw["evidence"], "change_request_assessment.evidence"
    )
    if len(raw_evidence) > 8:
        raise ValueError("change_request_assessment.evidence must contain at most 8 items")
    checked_evidence = [
        require_string(item, f"change_request_assessment.evidence[{index}]")
        for index, item in enumerate(raw_evidence)
    ]
    if len(set(checked_evidence)) != len(checked_evidence):
        raise ValueError("change_request_assessment.evidence must not contain duplicates")
    evidence = [
        text_or_default(
            item,
            "现有证据不足，无法进一步确认贡献者声明。",
        )
        for item in checked_evidence
    ]
    if not evidence:
        evidence = ["现有证据不足，无法进一步确认贡献者声明。"]
    return {
        "status": status,
        "contributor_goal": text_or_default(
            contributor_goal, "当前上下文未提供可确认的贡献者目标。"
        ),
        "expected_behavior": text_or_default(
            expected_behavior, "当前上下文未提供可确认的预期行为。"
        ),
        "implementation_summary": text_or_default(
            implementation_summary, "本轮依据代码差异完成了辅助审查。"
        ),
        "evidence": evidence,
    }


def build_changed_files(
    analysis: dict[str, Any], manifest: list[dict[str, str]], commands: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    semantic_by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(
        require_array(analysis["changed_files"], "changed_files")
    ):
        location = f"changed_files[{index}]"
        raw = require_object(value, location)
        require_exact_keys(raw, CHANGED_FILE_KEYS, location)
        file_id = require_string(raw["file_id"], f"{location}.file_id")
        if FILE_ID_RE.fullmatch(file_id) is None:
            raise ValueError(f"{location}.file_id is invalid")
        if file_id in semantic_by_id:
            raise ValueError(f"duplicate changed_files file_id: {file_id}")
        for key in {"summary", "impact", "validation_strategy"}:
            require_string(raw[key], f"{location}.{key}")
        semantic_by_id[file_id] = raw
    expected_ids = {f"FILE-{index:03d}" for index in range(1, len(manifest) + 1)}
    actual_ids = set(semantic_by_id)
    if actual_ids != expected_ids:
        raise ValueError(
            "changed_files does not cover the Git manifest; "
            f"missing={sorted(expected_ids - actual_ids)}, "
            f"unexpected={sorted(actual_ids - expected_ids)}"
        )
    changed_files = []
    for index, item in enumerate(manifest, start=1):
        semantic = semantic_by_id[f"FILE-{index:03d}"]
        changed_files.append(
            {
                "path": item["path"],
                "change_type": item["change_type"],
                "summary": text_or_default(
                    semantic.get("summary"),
                    "本轮 Codex AI 自动审查已覆盖该变更文件。",
                ),
                "impact": text_or_default(
                    semantic.get("impact"),
                    "具体影响已汇总在审查摘要、关键问题和剩余风险中。",
                ),
                "validation_strategy": text_or_default(
                    semantic.get("validation_strategy"),
                    (
                        "结合代码差异和本轮已执行的验证命令进行检查。"
                        if commands
                        else "未执行：本轮依据代码差异和已有 CI 证据完成审查。"
                    ),
                ),
            }
        )
    return changed_files


def build_behavior_coverage(analysis: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw_coverage = require_object(analysis["behavior_coverage"], "behavior_coverage")
    require_exact_keys(raw_coverage, set(BEHAVIOR_LABELS), "behavior_coverage")
    result: dict[str, dict[str, str]] = {}
    for name, label in BEHAVIOR_LABELS.items():
        location = f"behavior_coverage.{name}"
        raw = require_object(raw_coverage[name], location)
        require_exact_keys(raw, BEHAVIOR_ITEM_KEYS, location)
        for key in BEHAVIOR_ITEM_KEYS:
            require_string(raw[key], f"{location}.{key}")
        result[name] = {
            "scope": text_or_default(
                raw.get("scope"), f"检查本次差异涉及的{label}。"
            ),
            "strategy": text_or_default(
                raw.get("strategy"),
                "结合代码差异、已有 CI 证据和本轮定向命令进行审查。",
            ),
            "result": text_or_default(
                raw.get("result"),
                "具体结果已汇总在审查摘要、关键问题和剩余风险中。",
            ),
        }
    return result


def build_findings(
    analysis: dict[str, Any],
    file_by_id: dict[str, dict[str, str]],
    repository_root: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, value in enumerate(
        require_array(analysis["findings"], "findings"), start=1
    ):
        location = f"findings[{index - 1}]"
        raw = require_object(value, location)
        require_exact_keys(raw, FINDING_KEYS, location)
        file_id = require_string(raw["file_id"], f"{location}.file_id")
        trusted = file_by_id.get(file_id)
        if (
            FILE_ID_RE.fullmatch(file_id) is None
            or trusted is None
            or trusted["change_type"] == "deleted"
        ):
            raise ValueError(f"{location}.file_id must identify a retained changed file")
        line = validate_line(
            raw["line"], f"{location}.line", trusted["path"], repository_root
        )
        severity = require_string(raw["severity"], f"{location}.severity")
        category = require_string(raw["category"], f"{location}.category")
        if severity not in SEVERITIES:
            raise ValueError(f"{location}.severity is invalid")
        if category not in CATEGORIES:
            raise ValueError(f"{location}.category is invalid")
        for key in {"code_role", "title", "evidence", "impact", "fix_direction"}:
            require_string(raw[key], f"{location}.{key}")
        findings.append(
            {
                "id": f"AI-{index:03d}",
                "severity": severity,
                "category": category,
                "file": trusted["path"],
                "line": line,
                "code_role": text_or_default(raw.get("code_role"), "该位置参与本次变更行为。"),
                "title": text_or_default(raw.get("title"), "需要检查的代码问题"),
                "evidence": text_or_default(raw.get("evidence"), "Codex 未提供完整问题证据。"),
                "impact": text_or_default(raw.get("impact"), "可能影响本次变更涉及的行为。"),
                "fix_direction": text_or_default(raw.get("fix_direction"), "请结合该位置补充修复并复测。"),
            }
        )
    return findings


def build_suggested_tests(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, value in enumerate(
        require_array(analysis["suggested_tests"], "suggested_tests"), start=1
    ):
        location = f"suggested_tests[{index - 1}]"
        raw = require_object(value, location)
        require_exact_keys(raw, SUGGESTED_TEST_KEYS, location)
        priority = require_string(raw["priority"], f"{location}.priority")
        if priority not in SEVERITIES:
            raise ValueError(f"{location}.priority is invalid")
        target = require_string(raw["target"], f"{location}.target")
        description = require_string(raw["description"], f"{location}.description")
        result.append(
            {
                "id": f"TEST-{index:03d}",
                "priority": priority,
                "target": target.strip() or "未指定目标",
                "description": text_or_default(description, "建议补充相关定向测试。"),
            }
        )
    return result


def build_report(args: argparse.Namespace) -> None:
    raw_analysis = load_json(args.analysis)
    analysis = require_object(raw_analysis, "analysis root")
    require_exact_keys(analysis, ANALYSIS_KEYS, "analysis root")
    summary = require_string(analysis["summary"], "summary")
    merge_recommendation = require_string(
        analysis["merge_recommendation"], "merge_recommendation"
    )
    manifest = load_manifest(args.manifest)
    file_by_id = {
        f"FILE-{index:03d}": item for index, item in enumerate(manifest, start=1)
    }
    ledger = load_command_ledger(args.command_ledger)
    generated_files = [
        relative
        for relative in parse_generated_archive(args.generated_archive)
        if is_test_path(relative)
    ]
    commands, _, annotated_command_count = build_commands(analysis, ledger)

    assessment = require_object(analysis["test_assessment"], "test_assessment")
    require_exact_keys(assessment, TEST_ASSESSMENT_KEYS, "test_assessment")
    evidence_level = require_string(
        assessment["evidence_level"], "test_assessment.evidence_level"
    )
    if evidence_level not in EVIDENCE_LEVELS:
        raise ValueError("test_assessment.evidence_level is invalid")
    raw_execution_summary = require_array(
        assessment["summary"], "test_assessment.summary"
    )
    if len(raw_execution_summary) > 8:
        raise ValueError("test_assessment.summary must contain at most 8 items")
    checked_execution_summary = [
        require_string(item, f"test_assessment.summary[{index}]")
        for index, item in enumerate(raw_execution_summary)
    ]
    if len(set(checked_execution_summary)) != len(checked_execution_summary):
        raise ValueError("test_assessment.summary must not contain duplicates")
    execution_summary = [
        text_or_default(
            item,
            "本轮验证说明未完整提供。",
        )
        for item in checked_execution_summary
    ]
    if not execution_summary:
        execution_summary = [
            "本轮未执行额外命令。"
            if not commands
            else "执行结果由 runner 从可信 Codex JSONL 事件生成。"
        ]

    findings = build_findings(analysis, file_by_id, args.repository_root)
    suggested_tests = build_suggested_tests(analysis)
    execution_status = derive_execution_status(
        evidence_level,
        commands,
        annotated_command_count=annotated_command_count,
        has_suggested_tests=bool(suggested_tests),
    )
    residual_risks = [
        text_or_default(
            require_string(item, f"residual_risks[{index}]"),
            "仍有未覆盖的审查风险。",
        )
        for index, item in enumerate(
            require_array(analysis["residual_risks"], "residual_risks")
        )
    ]
    changed_files = build_changed_files(analysis, manifest, commands)
    behavior_coverage = build_behavior_coverage(analysis)
    verdict = (
        "FAIL"
        if any(finding["severity"] == "HIGH" for finding in findings)
        else "WARNING"
        if findings or execution_status in WARNING_EXECUTION_STATUSES
        else "PASS"
    )
    report = {
        "verdict": verdict,
        "summary": text_or_default(
            summary, "本轮已完成代码差异的 Codex AI 自动审查。"
        ),
        "merge_recommendation": text_or_default(
            merge_recommendation,
            "请结合本地确定性 CI 检查结果决定是否合入。",
        ),
        "change_request_assessment": normalize_assessment(analysis),
        "changed_files": changed_files,
        "behavior_coverage": behavior_coverage,
        "findings": findings,
        "suggested_tests": suggested_tests,
        "residual_risks": residual_risks,
        "test_execution": {
            "status": execution_status,
            "summary": execution_summary,
            "generated_test_files": generated_files,
            "commands": commands,
        },
        "completion_marker": "CODEX_AI_CI_COMPLETE",
    }
    write_json(args.output, report)


def parse_bool(value: str) -> bool:
    if value not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return value == "true"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-manifest")
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--analysis", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--command-ledger", type=Path, required=True)
    build.add_argument("--generated-archive", type=Path, required=True)
    build.add_argument("--repository-root", type=Path, required=True)
    build.add_argument("--analysis-mode", choices=("full", "analysis_only"), required=True)
    build.add_argument("--test-generation-expected", type=parse_bool, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "prepare-manifest":
            prepare_manifest(args.input, args.output)
        else:
            build_report(args)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, tarfile.TarError) as exc:
        print(f"Invalid Codex AI analysis: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
