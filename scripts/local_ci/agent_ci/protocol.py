"""Versioned identities shared by the worker, tools and result receiver."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

TASK_SCHEMA = "triton-anchor-local-ci-task/v4"
RESULT_SCHEMA = "triton-anchor-local-ci/v4"
POLICY_VERSION = "impact/v4"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_FIELDS = (
    "repository", "event_kind", "pr_number", "target_branch", "tested_sha",
    "base_sha", "head_sha", "worker_revision_sha", "metadata_digest", "full",
)


class ContractError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def metadata_digest(task: dict) -> str:
    return digest({key: sorted(task[key]) if key == "labels" else task[key]
                   for key in ("title", "description", "labels", "state", "draft")})


def task_id(task: dict) -> str:
    return digest({key: task[key] for key in IDENTITY_FIELDS})


def current_key(task: dict) -> str:
    subject = f"pr:{task['pr_number']}" if task["pr_number"] else f"branch:{task['target_branch']}"
    return hashlib.sha256(f"{task['repository']}:{subject}".encode()).hexdigest()


def validate_task(task: dict, repositories: tuple[str, ...] = ("likehupochuan/triton-anchor",)) -> dict:
    if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
        raise ContractError("Only complete v4 tasks may execute; unsupported schemas cannot satisfy the gate")
    required = {*IDENTITY_FIELDS, "task_id", "task_ref", "base_task_ref", "head_task_ref",
                "title", "description", "labels", "state", "draft", "captured_at", "llvm_hash"}
    if required - task.keys():
        raise ContractError(f"Missing task fields: {sorted(required - task.keys())}")
    if task["repository"] not in repositories or task["repository"] not in {"likehupochuan/triton-anchor", "anteloper-c/triton-anchor"}:
        raise ContractError("Repository is outside the configured allowlist")
    if task["event_kind"] not in {"pull_request", "push", "manual"}:
        raise ContractError("Unsupported event kind")
    if type(task["pr_number"]) is not int or task["pr_number"] < 0:
        raise ContractError("Invalid PR number")
    for key in ("tested_sha", "base_sha", "head_sha", "worker_revision_sha", "llvm_hash"):
        if not isinstance(task[key], str) or not SHA.fullmatch(task[key]):
            raise ContractError(f"Invalid {key}")
    for key in ("task_id", "metadata_digest"):
        if not isinstance(task[key], str) or not ID.fullmatch(task[key]):
            raise ContractError(f"Invalid {key}")
    if type(task["draft"]) is not bool or type(task["full"]) is not bool:
        raise ContractError("draft/full must be booleans")
    if not isinstance(task["labels"], list) or any(not isinstance(v, str) for v in task["labels"]):
        raise ContractError("labels must be strings")
    for key in ("title", "description", "target_branch", "captured_at"):
        if not isinstance(task[key], str) or not task[key].strip() or "\x00" in task[key]:
            raise ContractError(f"Invalid {key}")
    if task["target_branch"] == "CI_dev_forPR":
        raise ContractError("CI_dev_forPR is excluded from this deployment")
    for key in ("task_ref", "base_task_ref", "head_task_ref"):
        ref = task[key]
        if not isinstance(ref, str) or not ref.startswith("ci/") or any(
            part in ref for part in ("..", "@{", "\\", " ", "~", "^", ":", "?", "*", "[", "\x00", "\n", "\r")
        ) or ref.endswith(("/", ".", ".lock")):
            raise ContractError(f"Invalid {key}")
    if task["event_kind"] == "pull_request":
        if not task["pr_number"] or task["state"] != "open" or task["draft"]:
            raise ContractError("PR is not open and ready for testing")
        if not task["task_ref"].startswith(f"ci/pr-{task['pr_number']}/"):
            raise ContractError("PR/ref mismatch")
    if task["metadata_digest"] != metadata_digest(task) or task["task_id"] != task_id(task):
        raise ContractError("Task or metadata identity mismatch")
    return task


def within(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ContractError("Expected a relative task path")
    base = root.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path == base:
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
