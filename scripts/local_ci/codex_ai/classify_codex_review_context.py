#!/usr/bin/env python3
"""Classify changed files into a lightweight Codex review context."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def group_for(path: str) -> str:
    if path.startswith("scripts/local_ci/codex_ai/"):
        return "codex_ai"
    if (
        path in {"README.md", "ROADMAP.md", "SECURITY.md"}
        or path.startswith("docs/")
        or path.endswith((".md", ".markdown", ".rst"))
    ):
        return "docs"
    if path.startswith("scripts/local_ci/deterministic_ci/performance/"):
        return "performance"
    if path.startswith("scripts/dashboard/") or path.startswith("dashboard/"):
        return "dashboard"
    if path.startswith("scripts/local_ci/results/"):
        return "results_bridge"
    if path.startswith("scripts/local_ci/shared/"):
        return "shared_protocol"
    if path.startswith("scripts/local_ci/"):
        return "local_ci_control"
    if path.startswith(".github/workflows/"):
        return "github_workflows"
    if path.startswith("python/triton_anchor/"):
        return "python_frontend"
    if path.startswith("csrc/") or path.startswith("triton/"):
        return "compiler_core"
    if (
        "/tests/" in path
        or path.startswith("tests/")
        or path.endswith("_test.py")
        or path.startswith("scripts/local_ci/tests/")
    ):
        return "tests"
    return "other"


def classify_review_context(
    manifest: Any, analysis_mode: str
) -> tuple[str, str, dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    change_types: dict[str, int] = {}
    for item in manifest if isinstance(manifest, list) else []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        change_type = item.get("change_type")
        if not isinstance(path, str) or not isinstance(change_type, str):
            continue
        groups.setdefault(group_for(path), []).append(path)
        change_types[change_type] = change_types.get(change_type, 0) + 1

    count = sum(len(paths) for paths in groups.values())
    keys = set(groups)
    control_groups = {
        "local_ci_control",
        "shared_protocol",
        "results_bridge",
        "github_workflows",
        "tests",
        "docs",
    }

    if count == 0:
        profile = "empty_diff"
        hint = "未检测到变更文件；只确认任务元数据和结果协议，不生成测试。"
    elif keys <= {"codex_ai", "tests", "docs"} and "codex_ai" in keys:
        profile = "codex_ai_ci_maintenance"
        hint = (
            "本次仅涉及 Codex AI-CI 自身文件；不纳入 triton-anchor 产品审查。"
            "只做变更清单覆盖和摘要记录，不生成产品 finding，验证应依赖专用 "
            "prompt/schema/renderer 契约测试和人工维护审查。"
        )
    elif analysis_mode == "analysis_only":
        profile = "local_ci_failure"
        hint = (
            "确定性 Local CI 已失败；优先阅读 delivery-summary、result.json 和失败阶段"
            "日志，只在需要归因时展开相关 diff，避免读取无关大日志。"
        )
    elif keys <= {"docs"}:
        profile = "docs_only"
        hint = "本次为纯文档改动；跳过测试生成，轻量检查文档是否与当前代码和 Local CI 协议相符。"
    elif keys <= {"results_bridge", "shared_protocol", "tests", "docs"} and {
        "results_bridge",
        "shared_protocol",
    } & keys:
        profile = "local_ci_protocol"
        hint = (
            "本次集中在 Local CI 结果协议或桥接逻辑；重点检查 task ref、SHA/run ID、"
            "结果目录、manifest、status 回写和发布失败重试。"
        )
    elif keys <= {"performance", "dashboard", "tests", "docs"} and {
        "performance",
        "dashboard",
    } & keys:
        profile = "performance"
        hint = (
            "本次集中在性能测量或 dashboard；重点检查 benchmark schema、compare 语义、"
            "cache namespace、warning 映射和 dashboard artifact。"
        )
    elif count > 20:
        profile = "large_diff"
        hint = (
            "本次 diff 较大；先使用文件分组识别高风险模块，再按风险展开关键文件，"
            "避免逐行阅读低风险重复内容。"
        )
    elif keys <= control_groups:
        profile = "local_ci_control"
        hint = (
            "本次集中在 Local CI 控制面；重点检查 task ref、SHA、结果目录、状态字段、"
            "token 边界和发布失败重试；若涉及 GitHub workflows，顺带检查事件覆盖、"
            "workflow/artifact 契约和特权事件的不可信输入边界。"
        )
    else:
        profile = "general"
        hint = (
            "按标准项目专项审查执行；优先检查直接受 diff 影响的编译前端、CI 协议和"
            "测试证据。"
        )

    summary = {
        "schema": "triton-anchor-codex-review-context/v1",
        "profile": profile,
        "file_count": count,
        "groups": {key: sorted(paths) for key, paths in sorted(groups.items())},
        "change_types": change_types,
    }
    return profile, hint, summary


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: classify_codex_review_context.py MANIFEST_PATH ANALYSIS_MODE",
            file=sys.stderr,
        )
        return 2

    manifest_path, analysis_mode = sys.argv[1:3]
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = []

    profile, hint, summary = classify_review_context(manifest, analysis_mode)
    print(profile)
    print(hint)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
