"""Shared task identity and the small Local CI result format."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

TASK_SCHEMA = "triton-anchor-local-ci-task"
RESULT_SCHEMA = "triton-anchor-local-ci"
PREINSTALLED_SUBMODULES = frozenset({"FlagGems"})
SHA = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
IDENTITY_FIELDS = (
    "repository",
    "event_kind",
    "pr_number",
    "target_branch",
    "tested_sha",
    "base_sha",
    "head_sha",
    "worker_revision_sha",
    "metadata_digest",
    "full",
)
RESULT_STATUSES = {"pass", "fail", "infra_error", "cancelled"}
CHECK_STATUSES = RESULT_STATUSES | {"not_selected", "not_applicable", "skipped"}


class ContractError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def metadata_digest(task: dict) -> str:
    return digest(
        {
            key: sorted(task[key]) if key == "labels" else task[key]
            for key in ("title", "description", "labels", "state", "draft")
        }
    )


def task_id(task: dict) -> str:
    return digest({key: task[key] for key in IDENTITY_FIELDS})


def current_key(task: dict) -> str:
    subject = (
        f"pr:{task['pr_number']}"
        if task["pr_number"]
        else f"branch:{task['target_branch']}"
    )
    return hashlib.sha256(f"{task['repository']}:{subject}".encode()).hexdigest()


def is_legacy_task(task: dict) -> bool:
    return isinstance(task, dict) and str(task.get("schema", "")).startswith(
        TASK_SCHEMA + "/"
    )


def validate_task(
    task: dict,
    repositories=("likehupochuan/triton-anchor", "anteloper-c/triton-anchor"),
) -> dict:
    if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
        raise ContractError("Unsupported task format")
    required = {
        *IDENTITY_FIELDS,
        "task_id",
        "task_ref",
        "base_task_ref",
        "head_task_ref",
        "title",
        "description",
        "labels",
        "state",
        "draft",
        "captured_at",
        "llvm_hash",
    }
    if required - task.keys():
        raise ContractError("Task is missing required identity fields")
    if task["repository"] not in repositories:
        raise ContractError("Task repository is not configured for this worker")
    if type(task["pr_number"]) is not int or task["pr_number"] < 0:
        raise ContractError("Invalid PR number")
    if task["event_kind"] not in {"pull_request", "push", "manual"} or (
        (task["event_kind"] == "pull_request") != bool(task["pr_number"])
    ):
        raise ContractError("Task event and PR identity disagree")
    for key in (
        "tested_sha",
        "base_sha",
        "head_sha",
        "worker_revision_sha",
        "llvm_hash",
    ):
        if not isinstance(task[key], str) or not SHA.fullmatch(task[key]):
            raise ContractError(f"Invalid {key}")
    if type(task["draft"]) is not bool or type(task["full"]) is not bool:
        raise ContractError("Task draft/full must be booleans")
    if not isinstance(task["labels"], list) or any(
        not isinstance(label, str) for label in task["labels"]
    ):
        raise ContractError("Task labels must be strings")
    for key in ("target_branch", "title", "description", "state", "captured_at"):
        if not isinstance(task[key], str):
            raise ContractError(f"Invalid task {key}")
    if task["metadata_digest"] != metadata_digest(task) or task["task_id"] != task_id(
        task
    ):
        raise ContractError("Task or metadata identity mismatch")
    prefix = (
        f"ci/pr-{task['pr_number']}/{task['task_id']}"
        if task["pr_number"]
        else f"ci/branch/{task['task_id']}"
    )
    for key, suffix in (
        ("task_ref", "tested"),
        ("base_task_ref", "base"),
        ("head_task_ref", "head"),
    ):
        if task[key] != f"{prefix}/{suffix}":
            raise ContractError("Source refs must belong to the frozen task")
    return task


def within(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ContractError("Expected a relative task path")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ContractError("Path escapes task workspace")
    if must_exist and not path.is_file():
        raise ContractError("Task file does not exist")
    return path


def atomic_json(path: Path, value: Any) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_result(result: dict, expected_task: dict | None = None) -> dict:
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        raise ContractError("Unsupported result format")
    task = validate_task(result.get("task"))
    if expected_task is not None and task != expected_task:
        raise ContractError("Result belongs to a different dispatched task")
    if not isinstance(result.get("run_id"), str) or not RUN_ID.fullmatch(
        result["run_id"]
    ):
        raise ContractError("Invalid result run id")
    if result.get("status") not in RESULT_STATUSES or not isinstance(
        result.get("summary"), str
    ):
        raise ContractError("Result needs a terminal status and summary")
    for name in ("checks", "reviews", "findings", "blocking_reasons", "artifacts"):
        if not isinstance(result.get(name), list):
            raise ContractError(f"Result {name} must be a list")
    for name in ("policy", "environment"):
        if not isinstance(result.get(name), dict):
            raise ContractError(f"Result {name} must be an object")
    for name, identity in (("checks", "tool_id"), ("reviews", "kind")):
        for row in result[name]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get(identity), str)
                or row.get("status") not in CHECK_STATUSES
            ):
                raise ContractError(f"Invalid {identity} result")
    for row in result["artifacts"]:
        if not isinstance(row, dict):
            raise ContractError("Invalid artifact entry")
        within(Path("/artifacts"), row.get("path", ""))
    if result["status"] == "pass" and result["blocking_reasons"]:
        raise ContractError("Passing result contains blocking reasons")
    return result
