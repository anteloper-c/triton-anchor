"""Versioned identities shared by the worker, tools and result receiver."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

TASK_SCHEMA = "triton-anchor-local-ci-task/v4"
RESULT_SCHEMA = "triton-anchor-local-ci/v4"
POLICY_VERSION = "impact/v5"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[0-9a-f]{64}\Z")
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


def validate_task(
    task: dict, repositories: tuple[str, ...] = ("likehupochuan/triton-anchor",)
) -> dict:
    if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
        raise ContractError(
            "Only complete v4 tasks may execute; unsupported schemas cannot satisfy the gate"
        )
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
        raise ContractError(f"Missing task fields: {sorted(required - task.keys())}")
    if task["repository"] not in repositories or task["repository"] not in {
        "likehupochuan/triton-anchor",
        "anteloper-c/triton-anchor",
    }:
        raise ContractError("Repository is outside the configured allowlist")
    if task["event_kind"] not in {"pull_request", "push", "manual"}:
        raise ContractError("Unsupported event kind")
    if type(task["pr_number"]) is not int or task["pr_number"] < 0:
        raise ContractError("Invalid PR number")
    for key in (
        "tested_sha",
        "base_sha",
        "head_sha",
        "worker_revision_sha",
        "llvm_hash",
    ):
        if not isinstance(task[key], str) or not SHA.fullmatch(task[key]):
            raise ContractError(f"Invalid {key}")
    for key in ("task_id", "metadata_digest"):
        if not isinstance(task[key], str) or not ID.fullmatch(task[key]):
            raise ContractError(f"Invalid {key}")
    if type(task["draft"]) is not bool or type(task["full"]) is not bool:
        raise ContractError("draft/full must be booleans")
    if not isinstance(task["labels"], list) or any(
        not isinstance(v, str) for v in task["labels"]
    ):
        raise ContractError("labels must be strings")
    for key in ("title", "description", "target_branch", "captured_at"):
        if (
            not isinstance(task[key], str)
            or not task[key].strip()
            or "\x00" in task[key]
        ):
            raise ContractError(f"Invalid {key}")
    for key in ("task_ref", "base_task_ref", "head_task_ref"):
        ref = task[key]
        if (
            not isinstance(ref, str)
            or not ref.startswith("ci/")
            or any(
                part in ref
                for part in (
                    "..",
                    "@{",
                    "\\",
                    " ",
                    "~",
                    "^",
                    ":",
                    "?",
                    "*",
                    "[",
                    "\x00",
                    "\n",
                    "\r",
                )
            )
            or ref.endswith(("/", ".", ".lock"))
        ):
            raise ContractError(f"Invalid {key}")
    modules = task.get("submodules", [])
    if not isinstance(modules, list):
        raise ContractError("Submodule manifest must be a list")
    import urllib.parse

    seen = set()
    for module in modules:
        if not isinstance(module, dict) or module.get("variant") not in {
            "candidate",
            "base",
        }:
            raise ContractError("Invalid submodule variant")
        within(Path("/source"), module.get("path", ""))
        identity = (module["variant"], module["path"])
        if identity in seen or not SHA.fullmatch(str(module.get("sha", ""))):
            raise ContractError("Invalid or duplicate pinned submodule")
        seen.add(identity)
        parsed = urllib.parse.urlsplit(module.get("repository_url", ""))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "gitee.com"
            or parsed.username
            or parsed.password
        ):
            raise ContractError("Submodule mirror must be HTTPS Gitee")
        if (
            not str(module.get("task_ref", "")).startswith("ci/")
            or task["task_id"] not in module["task_ref"]
        ):
            raise ContractError("Submodule source ref must belong to the task")
    if task["event_kind"] == "pull_request":
        if not task["pr_number"] or task["state"] != "open" or task["draft"]:
            raise ContractError("PR is not open and ready for testing")
        if not task["task_ref"].startswith(f"ci/pr-{task['pr_number']}/"):
            raise ContractError("PR/ref mismatch")
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
            raise ContractError(
                "Source refs must be immutable and specific to this task"
            )
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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_artifacts(result: dict) -> dict[str, dict]:
    """Validate logical sealed evidence identities independently of their URLs."""
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or not ID.fullmatch(
        str(result.get("execution_summary_sha256", ""))
    ):
        raise ContractError("Result lacks its sealed artifact/command manifest")
    identities = {}
    for artifact in artifacts:
        identity = artifact.get("artifact_id") if isinstance(artifact, dict) else None
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", identity)
            or identity in identities
        ):
            raise ContractError("Invalid or duplicate artifact identity")
        if type(artifact.get("required")) is not bool:
            raise ContractError("Artifact required flag must be explicit")
        if artifact.get("omitted"):
            if artifact["required"]:
                raise ContractError(
                    "Necessary evidence cannot be omitted from a sealed result"
                )
        else:
            if (
                not ID.fullmatch(str(artifact.get("sha256", "")))
                or type(artifact.get("size")) is not int
                or artifact["size"] < 0
            ):
                raise ContractError("Artifact must record its digest and size")
            within(Path("/sealed"), artifact.get("path", ""))
        identities[identity] = artifact
    return identities


def validate_execution_summary(result: dict, summary: dict) -> None:
    artifacts = validate_artifacts(result)
    if (
        summary.get("schema") != "triton-anchor-executions/v1"
        or summary.get("task_id") != result["task"]["task_id"]
        or summary.get("run_id") != result["run_id"]
        or not isinstance(summary.get("executions"), list)
    ):
        raise ContractError("Execution summary belongs to a different task/run")
    records = {}
    for record in summary["executions"]:
        identity = record.get("execution_id")
        if not isinstance(identity, str) or identity in records:
            raise ContractError("Duplicate or missing execution identity")
        records[identity] = record
        for artifact_id in record.get("artifact_ids", []):
            artifact = artifacts.get(artifact_id)
            if not artifact or artifact.get("execution_id") != identity:
                raise ContractError(
                    "Execution/artifact association differs from sealed manifest"
                )
    if result["status"] != "pass":
        return
    required = set(result["required_checks"])
    environment = result.get("environment", {}).get("environment_fingerprint")
    for check in result["checks"]:
        if (
            check["tool_id"] not in required
            or check.get("variant", "candidate") != "candidate"
        ):
            continue
        identities = check.get("execution_ids") or [check.get("execution_id")]
        for identity in identities:
            record = records.get(identity, {})
            if record.get("status") != "pass" or record.get("exit_code") != 0:
                raise ContractError(
                    "Passing required check lacks a successful observed execution"
                )
            if (
                record.get("tool_id") != check["tool_id"]
                or record.get("variant") != "candidate"
                or record.get("tested_sha") != result["task"]["tested_sha"]
                or not environment
                or record.get("environment_fingerprint") != environment
                or record.get("original_subject") is not True
            ):
                raise ContractError(
                    "Required execution does not match its behavior, candidate or environment"
                )
            source_id = record.get("source_execution_id")
            if record.get("record_type") == "check_association" or source_id:
                source = records.get(source_id, {})
                if (
                    not source_id
                    or source_id == identity
                    or source.get("source_execution_id")
                    or source.get("execution_kind") not in {"native", "custom"}
                    or source.get("status") != "pass"
                    or source.get("exit_code") != 0
                    or any(
                        source.get(key) != record.get(key)
                        for key in (
                            "variant",
                            "tested_sha",
                            "environment_fingerprint",
                            "original_subject",
                        )
                    )
                ):
                    raise ContractError(
                        "Check association lacks its matching successful observed command"
                    )
            if not any(
                artifacts[key]["required"] and not artifacts[key].get("omitted")
                for key in record.get("artifact_ids", [])
            ):
                raise ContractError(
                    "Passing required check lacks necessary execution evidence"
                )


def validate_delivery(result: dict, result_digest: str, index: dict | None) -> str:
    artifacts = validate_artifacts(result)
    if index is None:
        return "pending"
    if (
        index.get("schema") != "triton-anchor-delivery/v1"
        or index.get("task_id") != result["task"]["task_id"]
        or index.get("run_id") != result["run_id"]
        or index.get("result_digest") != result_digest
    ):
        raise ContractError("Delivery index belongs to a different sealed result")
    rows = index.get("artifacts")
    if not isinstance(rows, list) or len(
        {row.get("artifact_id") for row in rows}
    ) != len(rows):
        raise ContractError("Invalid delivery artifact identities")
    delivered = {row["artifact_id"]: row for row in rows}
    ready, expired = True, False
    for identity, artifact in artifacts.items():
        row = delivered.get(identity, {})
        if row and row.get("required") != artifact["required"]:
            raise ContractError("Delivery changed artifact necessity")
        if row.get("status") == "ready":
            if (
                row.get("sha256") != artifact.get("sha256")
                or row.get("size") != artifact.get("size")
                or row.get("verified_sha256") != artifact.get("sha256")
                or not row.get("attachment_id")
                or not row.get("release_id")
            ):
                raise ContractError(
                    "Delivery lacks verified attachment identity/hash/size"
                )
        if artifact["required"] and row.get("status") != "ready":
            ready = False
            expired |= row.get("status") == "expired"
    if index.get("status") == "ready" and not ready:
        raise ContractError("Delivery claimed ready with missing necessary evidence")
    return "ready" if ready else "expired" if expired else "pending"


def scope_covers(scope: dict, required: dict) -> bool:
    """Compare report-derived execution coverage with the frozen policy floor."""
    if required.get("mode") and scope.get("mode") != required["mode"]:
        return False
    for key in ("ops", "kernels"):
        if required.get(key) and not set(required[key]) <= set(scope.get(key, [])):
            return False
    for selected in required.get("paths", []):
        filename, node = (selected.split("::", 1) + [""])[:2]
        stem = filename.removesuffix(".py").replace("/", ".")

        def matches(case):
            file = case.get("file", "").replace("\\", "/")
            classname = case.get("class", "")
            path_matches = (
                file == filename
                or file.endswith("/" + filename)
                or file.startswith(filename.rstrip("/") + "/")
                or classname == stem
                or classname.startswith(stem + ".")
            )
            if not path_matches or not node:
                return path_matches
            pieces = node.split("::")
            method = pieces[-1]
            observed_name = case.get("name", "")
            method_matches = (
                observed_name == method
                if "[" in method
                else observed_name.split("[", 1)[0] == method
            )
            expected_class = ".".join(pieces[:-1])
            class_matches = (
                not expected_class
                or classname == expected_class
                or classname.endswith("." + expected_class)
            )
            return method_matches and class_matches

        if not any(
            case.get("status") == "passed" and matches(case)
            for case in scope.get("observed_tests", [])
        ):
            return False
    return True
