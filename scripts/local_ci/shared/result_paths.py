#!/usr/bin/env python3
"""Canonical paths for local-CI results published to the results repository."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import PurePosixPath


def safe_path_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "default"


def result_task_dir(task_ref: str) -> PurePosixPath:
    normalized = task_ref.strip("/")

    match = re.fullmatch(r"ci/full/(.+)", normalized)
    if match:
        return PurePosixPath(
            "runs",
            "ci_full",
            f"ci_full_{safe_path_part(match.group(1))}",
        )

    match = re.fullmatch(r"ci/push/(.+)", normalized)
    if match:
        return PurePosixPath(
            "runs",
            "ci_push",
            f"ci_push_{safe_path_part(match.group(1))}",
        )

    match = re.fullmatch(r"ci/pr-([0-9]+)/(.+)", normalized)
    if match:
        return PurePosixPath(
            "runs",
            "ci_pr",
            f"ci_pr-{match.group(1)}_{safe_path_part(match.group(2))}",
        )

    match = re.fullmatch(r"ci/base/pr-([0-9]+)/(.+)", normalized)
    if match:
        return PurePosixPath(
            "runs",
            "ci_pr",
            f"ci_base_pr-{match.group(1)}_{safe_path_part(match.group(2))}",
        )

    raise ValueError(
        "Unsupported local-CI task ref. Expected ci/full/*, ci/push/*, "
        f"ci/pr-<number>/*, or ci/base/pr-<number>/*; got {task_ref!r}"
    )


def result_commit_dir(task_ref: str, sha: str, head_sha: str = "") -> PurePosixPath:
    normalized = task_ref.strip("/")
    if re.fullmatch(r"ci/pr-[0-9]+/.+", normalized):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise ValueError("PR result paths require a full head SHA")
        commit_part = f"h-{head_sha[:12]}_m-{sha[:12]}"
    else:
        commit_part = safe_path_part(sha)
    return result_task_dir(task_ref) / commit_part


def result_run_dir(
    task_ref: str, sha: str, run_id: str, head_sha: str = ""
) -> PurePosixPath:
    return result_commit_dir(task_ref, sha, head_sha) / safe_path_part(run_id)


def gitee_tree_url(web_url: str, ref: str, relative_path: str | PurePosixPath) -> str:
    quoted_ref = urllib.parse.quote(ref, safe="")
    quoted_path = urllib.parse.quote(str(relative_path), safe="/")
    return f"{web_url.rstrip('/')}/tree/{quoted_ref}/{quoted_path}"


def gitee_blob_url(web_url: str, ref: str, relative_path: str | PurePosixPath) -> str:
    quoted_ref = urllib.parse.quote(ref, safe="")
    quoted_path = urllib.parse.quote(str(relative_path), safe="/")
    return f"{web_url.rstrip('/')}/blob/{quoted_ref}/{quoted_path}"
