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

ROOT_KEYS = {
    "verdict",
    "summary",
    "findings",
    "suggested_tests",
    "residual_risks",
    "completion_marker",
}
FINDING_KEYS = {
    "id",
    "severity",
    "category",
    "file",
    "line",
    "title",
    "evidence",
    "impact",
    "fix_direction",
}
TEST_KEYS = {"id", "priority", "target", "description"}
SEVERITIES = {"HIGH", "MEDIUM", "LOW"}
CATEGORIES = {
    "correctness",
    "regression",
    "security",
    "api-compatibility",
    "test-gap",
    "other",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--changed-file-count", required=True, type=int)
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


def validate_report(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("report root must be an object")
    require_exact_keys(document, ROOT_KEYS, "report")

    verdict = require_string(document["verdict"], "verdict")
    if verdict not in {"PASS", "WARNING", "FAIL"}:
        raise ValueError(f"unsupported verdict: {verdict}")
    require_string(document["summary"], "summary")
    if document["completion_marker"] != "CODEX_AI_CI_COMPLETE":
        raise ValueError("completion_marker is invalid")

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
        for key in FINDING_KEYS - {"id", "severity", "category"}:
            require_string(finding[key], f"{location}.{key}")

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
        require_string(test["description"], f"{location}.description")

    residual_risks = document["residual_risks"]
    if not isinstance(residual_risks, list):
        raise ValueError("residual_risks must be an array")
    for index, risk in enumerate(residual_risks):
        require_string(risk, f"residual_risks[{index}]")

    expected_verdict = "FAIL" if any(
        finding["severity"] == "HIGH" for finding in findings
    ) else "WARNING" if findings else "PASS"
    if verdict != expected_verdict:
        raise ValueError(
            f"verdict {verdict} does not match findings; expected {expected_verdict}"
        )
    return document


def inline(value: Any) -> str:
    text = " ".join(str(value).split())
    return html.escape(text, quote=False).replace("|", "\\|").replace("`", "'")


def render_report(document: dict[str, Any], args: argparse.Namespace) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Codex AI CI Report",
        "",
        "## Metadata",
        "",
        "| Field | Value |",
        "| --- | --- |",
        "| Schema | `triton-anchor-codex-ai-report/v1` |",
        f"| Branch | `{inline(args.branch)}` |",
        f"| Base SHA | `{inline(args.base_sha)}` |",
        f"| Target SHA | `{inline(args.target_sha)}` |",
        f"| Changed files | {args.changed_file_count} |",
        f"| Generated at (UTC) | `{generated_at}` |",
        "",
        "## Verdict",
        "",
        f"**{document['verdict']}**",
        "",
        "## Summary",
        "",
        inline(document["summary"]),
        "",
        "## Findings",
        "",
    ]

    findings = document["findings"]
    if not findings:
        lines.extend(["No blocking findings.", ""])
    else:
        for finding in findings:
            location = f"{inline(finding['file'])}:{inline(finding['line'])}"
            lines.extend([
                f"### {finding['id']}: {inline(finding['title'])}",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| Severity | **{finding['severity']}** |",
                f"| Category | `{finding['category']}` |",
                f"| Location | `{location}` |",
                f"| Evidence | {inline(finding['evidence'])} |",
                f"| Impact | {inline(finding['impact'])} |",
                f"| Fix direction | {inline(finding['fix_direction'])} |",
                "",
            ])

    lines.extend(["## Suggested Tests", ""])
    tests = document["suggested_tests"]
    if not tests:
        lines.extend(["None suggested.", ""])
    else:
        lines.extend([
            "| ID | Priority | Target | Description |",
            "| --- | --- | --- | --- |",
        ])
        for test in tests:
            lines.append(
                f"| {test['id']} | **{test['priority']}** | "
                f"`{inline(test['target'])}` | {inline(test['description'])} |"
            )
        lines.append("")

    lines.extend(["## Residual Risks", ""])
    risks = document["residual_risks"]
    if risks:
        lines.extend(f"- {inline(risk)}" for risk in risks)
    else:
        lines.append("None reported.")
    lines.extend(["", "## Execution", "", "CODEX_AI_CI_COMPLETE", ""])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        input_path = Path(args.input)
        document = validate_report(json.loads(input_path.read_text(encoding="utf-8")))
        rendered = render_report(document, args)
        output_path = Path(args.output)
        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary_path.write_text(rendered, encoding="utf-8")
        temporary_path.replace(output_path)
        print(document["verdict"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"Invalid Codex AI report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
