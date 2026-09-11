"""Seal a concise result and selected evidence for a single Git publication."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .protocol import (
    CHECK_STATUSES,
    ContractError,
    RESULT_SCHEMA,
    RESULT_STATUSES,
    RUN_ID,
    atomic_json,
    validate_result,
    validate_task,
    within,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILES = 20
MAX_RESULT_BYTES = 2 * 1024 * 1024


def _redactor(redact):
    if redact is not None:
        return redact
    secrets = [
        value
        for key, value in os.environ.items()
        if len(value) > 3
        and any(
            word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
        )
    ]

    def clean(text):
        for value in secrets:
            text = text.replace(value, "[redacted]")
        return text

    return clean


def _clean(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_clean(item, redact) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, redact) for key, item in value.items()}
    return value


def _records(value, identity):
    if not isinstance(value, list):
        raise ContractError(f"Agent {identity} records must be a list")
    result = []
    seen = set()
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get(identity), str):
            raise ContractError(f"Agent record needs {identity}")
        if row[identity] in seen or row.get("status") not in CHECK_STATUSES:
            raise ContractError(f"Duplicate or invalid {identity} result")
        seen.add(row[identity])
        evidence = row.get("evidence", [])
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) for item in evidence
        ):
            raise ContractError("Evidence must be a list of paths or review references")
        result.append(
            {
                **row,
                "summary": str(row.get("summary", "")),
                "evidence": evidence,
            }
        )
    return result


def seal_result(
    task,
    run_id,
    agent_result,
    policy,
    environment,
    source_dir,
    destination,
    *,
    redact=None,
):
    """The host supplies identity; Codex supplies checks, reviews and selected files."""
    validate_task(task)
    if (
        not isinstance(run_id, str)
        or not RUN_ID.fullmatch(run_id)
        or not isinstance(agent_result, dict)
    ):
        raise ContractError("Invalid run or Agent result")
    redact = _redactor(redact)
    checks = _records(agent_result.get("checks", []), "tool_id")
    reviews = _records(agent_result.get("reviews", []), "kind")
    findings = agent_result.get("findings", [])
    if not isinstance(findings, list):
        raise ContractError("Findings must be a list")
    findings = list(findings)
    for review in reviews:
        for finding in review.get("findings", []):
            if finding not in findings:
                findings.append(finding)
    if any(not isinstance(finding, dict) for finding in findings):
        raise ContractError("Findings must be objects")
    reasons = agent_result.get("blocking_reasons", [])
    if not isinstance(reasons, list):
        raise ContractError("Blocking reasons must be a list")
    reasons = [str(reason) for reason in reasons]
    incomplete = False
    failed = False
    selected = {check["tool_id"]: check for check in checks}
    for tool_id in policy.get("required_checks", []):
        check = selected.get(tool_id, {})
        if check.get("status") != "pass":
            reasons.append(
                f"最低必检未通过：{tool_id}"
                + (f" — {check['summary']}" if check.get("summary") else "")
            )
            incomplete |= check.get("status") != "fail"
            failed |= check.get("status") == "fail"
    for check in checks:
        if check["status"] in {"fail", "infra_error", "cancelled"}:
            reason = f"{check['tool_id']}：{check['summary'] or check['status']}"
            if reason not in reasons:
                reasons.append(reason)
            failed |= check["status"] == "fail"
            incomplete |= check["status"] != "fail"
    reviewed = {review["kind"]: review for review in reviews}
    for kind in ("pr_info", "architecture"):
        review = reviewed.get(kind, {})
        if review.get("status") != "pass":
            reasons.append(
                f"必要审查未通过：{kind}"
                + (f" — {review['summary']}" if review.get("summary") else "")
            )
            failed |= review.get("status") == "fail"
            incomplete |= review.get("status") != "fail"
    for finding in findings:
        if finding.get("blocking") is True or finding.get("severity") in {
            "high",
            "critical",
        }:
            failed = True
            reasons.append(str(finding.get("summary", "高风险审查发现")))
    requested = agent_result.get("status", "pass")
    if requested not in RESULT_STATUSES:
        raise ContractError("Invalid Agent result status")
    failed |= requested == "fail" or bool(agent_result.get("blocking_reasons"))
    incomplete |= requested == "infra_error"
    if requested == "cancelled":
        status = "cancelled"
    elif failed:
        status = "fail"
    else:
        status = "infra_error" if incomplete else "pass"
    summary = str(agent_result.get("summary", ""))
    if status != "pass" and not reasons:
        reasons.append(summary or status)

    source = Path(source_dir) / "artifacts"
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    chosen = agent_result.get("artifacts", [])
    if not isinstance(chosen, list):
        raise ContractError("Agent artifacts must be a list")
    chosen = list(chosen) + [path for check in checks for path in check["evidence"]]
    artifacts = []
    seen = set()
    total = 0
    count = 0
    for entry in chosen:
        if not isinstance(entry, (str, dict)):
            raise ContractError("Artifact selection needs a relative path")
        path = entry if isinstance(entry, str) else entry.get("path", "")
        if path in seen:
            continue
        seen.add(path)
        incoming = within(source, path)
        row = {
            "path": path,
            "label": path if isinstance(entry, str) else str(entry.get("label", path)),
        }
        if not incoming.is_file():
            row["omitted"] = "文件未生成，未上传"
            for check in checks:
                if path in check["evidence"] and check["status"] == "pass":
                    check["status"] = "infra_error"
                    reasons.append(f"{check['tool_id']} 引用的证据文件不存在：{path}")
                    if status == "pass":
                        status = "infra_error"
        elif count >= MAX_FILES:
            row["omitted"] = "超出所选文件数量限制，保留在 CI 主机"
        elif incoming.stat().st_size > MAX_FILE_BYTES:
            row["omitted"] = "文件超过 2 MiB，保留在 CI 主机"
        else:
            data = incoming.read_bytes()
            try:
                data = redact(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
            if len(data) > MAX_FILE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
                row["omitted"] = "超出 10 MiB 发布预算，保留在 CI 主机"
            else:
                target = within(destination / "artifacts", redact(path))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                row["size"] = len(data)
                total += len(data)
                count += 1
        artifacts.append(row)
    result = {
        "schema": RESULT_SCHEMA,
        "task": task,
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "checks": checks,
        "reviews": reviews,
        "findings": findings,
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "artifacts": artifacts,
        "environment": environment,
        "policy": policy,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    result = _clean(result, redact)
    result["task"] = task
    validate_result(result, task)
    atomic_json(destination / "result.json", result)
    if (destination / "result.json").stat().st_size > MAX_RESULT_BYTES:
        raise ContractError("Result summary exceeds 2 MiB")
    return result
