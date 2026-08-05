#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = "triton-anchor-local-ci-task-metadata/v1"
TITLE_LIMIT = 500
DESCRIPTION_LIMIT = 8000
MAX_INPUT_BYTES = 64 * 1024
TASK_REF_RE = re.compile(r"ci/pr-([0-9]+)/(.+)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
REQUIRED_FIELDS = {
    "schema",
    "task_ref",
    "target_sha",
    "pr_number",
    "title",
    "description",
    "captured_at",
    "title_truncated",
    "description_truncated",
}


class MetadataError(ValueError):
    pass


def fail(message: str) -> None:
    raise MetadataError(message)


def normalize_text(value: Any, field: str) -> tuple[str, bool]:
    if not isinstance(value, str):
        fail(f"{field} 必须是字符串")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    removed_nul = "\x00" in normalized
    return normalized.replace("\x00", ""), removed_nul


def validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        fail("captured_at 必须是非空 UTC 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("captured_at 不是有效的 ISO 8601 时间")
    if parsed.tzinfo is None:
        fail("captured_at 必须包含时区")
    if parsed.utcoffset() != timedelta(0):
        fail("captured_at 必须使用 UTC")
    return value


def validate_document(
    document: Any,
    *,
    expected_task_ref: str,
    expected_target_sha: str,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(document, dict):
        fail("元数据顶层必须是 JSON 对象")
    missing = sorted(REQUIRED_FIELDS - set(document))
    extra = sorted(set(document) - REQUIRED_FIELDS)
    if missing:
        fail(f"元数据缺少字段：{', '.join(missing)}")
    if extra:
        fail(f"元数据包含未知字段：{', '.join(extra)}")
    if document["schema"] != SCHEMA:
        fail(f"schema 必须是 {SCHEMA}")
    if document["task_ref"] != expected_task_ref:
        fail("task_ref 与当前 PR 任务不一致")
    if document["target_sha"] != expected_target_sha:
        fail("target_sha 与当前 PR head SHA 不一致")
    if not SHA_RE.fullmatch(expected_target_sha):
        fail("期望的 target SHA 不是 40 位小写十六进制")

    task_match = TASK_REF_RE.fullmatch(expected_task_ref)
    if not task_match:
        fail("元数据只能用于 ci/pr-<number>/<branch> 任务")
    expected_pr_number = int(task_match.group(1))
    if isinstance(document["pr_number"], bool) or not isinstance(
        document["pr_number"], int
    ):
        fail("pr_number 必须是整数")
    if document["pr_number"] != expected_pr_number:
        fail("pr_number 与 task_ref 不一致")
    if not isinstance(document["title_truncated"], bool) or not isinstance(
        document["description_truncated"], bool
    ):
        fail("截断标记必须是布尔值")

    title, title_had_nul = normalize_text(document["title"], "title")
    description, description_had_nul = normalize_text(
        document["description"], "description"
    )
    title = title.strip()
    description = description.strip()
    if not title:
        fail("title 不能为空")

    warnings: list[str] = []
    if title_had_nul or description_had_nul:
        warnings.append("已移除标题或描述中的 NUL 字符")
    title_truncated = document["title_truncated"] or len(title) > TITLE_LIMIT
    description_truncated = (
        document["description_truncated"] or len(description) > DESCRIPTION_LIMIT
    )
    if len(title) > TITLE_LIMIT:
        warnings.append(f"title 超过 {TITLE_LIMIT} 字符，已截断")
    if len(description) > DESCRIPTION_LIMIT:
        warnings.append(f"description 超过 {DESCRIPTION_LIMIT} 字符，已截断")

    canonical = {
        "schema": SCHEMA,
        "task_ref": expected_task_ref,
        "target_sha": expected_target_sha,
        "pr_number": expected_pr_number,
        "title": title[:TITLE_LIMIT],
        "description": description[:DESCRIPTION_LIMIT],
        "captured_at": validate_timestamp(document["captured_at"]),
        "title_truncated": title_truncated,
        "description_truncated": description_truncated,
    }
    return canonical, warnings


def read_document(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            fail(f"元数据文件超过 {MAX_INPUT_BYTES} 字节上限")
        return json.loads(path.read_text(encoding="utf-8"))
    except MetadataError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(f"无法读取有效的 UTF-8 JSON：{path}")


def write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并规范化 Local CI PR 元数据")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--task-ref", required=True)
    parser.add_argument("--target-sha", required=True)
    args = parser.parse_args()

    try:
        canonical, warnings = validate_document(
            read_document(Path(args.input)),
            expected_task_ref=args.task_ref,
            expected_target_sha=args.target_sha,
        )
        write_document(Path(args.output), canonical)
    except MetadataError as exc:
        print(f"PR 元数据校验失败：{exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"PR 元数据警告：{warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
