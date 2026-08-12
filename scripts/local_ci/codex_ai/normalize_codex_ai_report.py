#!/usr/bin/env python3
"""Conservatively normalize mechanical Codex report contradictions."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


EXECUTED_COMMAND_STATUSES = {
    "passed",
    "stable_failure",
    "flaky_failure",
    "infrastructure_failure",
}
NORMALIZATION_NOTE = (
    "报告记录了已执行命令，整体状态已从未执行保守归一化为证据不足。"
)


def normalize_document(document: Any) -> list[str]:
    """Repair only contradictions whose conservative meaning is unambiguous."""
    if not isinstance(document, dict):
        return []

    execution = document.get("test_execution")
    if not isinstance(execution, dict) or execution.get("status") != "not_run":
        return []

    commands = execution.get("commands")
    if not isinstance(commands, list):
        return []
    has_executed_command = any(
        isinstance(command, dict)
        and command.get("status") in EXECUTED_COMMAND_STATUSES
        for command in commands
    )
    if not has_executed_command:
        return []

    changes = [
        "test_execution.status: not_run -> insufficient_evidence "
        "because test_execution.commands contains an executed command"
    ]
    execution["status"] = "insufficient_evidence"

    summary = execution.get("summary")
    if isinstance(summary, str) and summary.strip():
        execution["summary"] = [summary, NORMALIZATION_NOTE]
    elif (
        isinstance(summary, list)
        and NORMALIZATION_NOTE not in summary
        and len(summary) < 8
    ):
        summary.append(NORMALIZATION_NOTE)

    findings = document.get("findings")
    if isinstance(findings, list):
        expected_verdict = (
            "FAIL"
            if any(
                isinstance(finding, dict) and finding.get("severity") == "HIGH"
                for finding in findings
            )
            else "WARNING"
        )
        if document.get("verdict") != expected_verdict:
            changes.append(
                f"verdict: {document.get('verdict')} -> {expected_verdict} "
                "to match the normalized execution status"
            )
            document["verdict"] = expected_verdict

    return changes


def write_json_atomically(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    try:
        document = json.loads(input_path.read_text(encoding="utf-8"))
        changes = normalize_document(document)
        if changes:
            write_json_atomically(input_path, document)
            for change in changes:
                print(f"Normalized Codex AI report: {change}")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"Could not normalize Codex AI report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
