#!/usr/bin/env python3
"""Canonical paths for local-CI results published to the results repository."""

from __future__ import annotations

import re
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


def result_commit_dir(task_ref: str, sha: str) -> PurePosixPath:
    return result_task_dir(task_ref) / safe_path_part(sha)


def result_run_dir(task_ref: str, sha: str, run_id: str) -> PurePosixPath:
    return result_commit_dir(task_ref, sha) / safe_path_part(run_id)
