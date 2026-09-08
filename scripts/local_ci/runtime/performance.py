"""Choose performance baselines from host configuration or published host evidence."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from .common import read_json
from .relay import evidence_path


TOOLS = ("compile_time", "pass_profile", "ir_serialization")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_BASELINE_BYTES = 8 * 1024 * 1024


def _identity(profile, task):
    return {"base_sha": task["base_sha"], "profile_id": profile["id"],
            "llvm_revision": profile["llvm_revision"]}


def _matches(record, identity):
    return isinstance(record, dict) and all(record.get(key) == value for key, value in identity.items())


def _container_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _successful_tool(result, tool):
    checks = [item for item in result.get("checks", []) if isinstance(item, dict) and item.get("id") == tool]
    if len(checks) != 1 or checks[0].get("status") != "passed":
        return False
    receipts = {item.get("id"): item for item in result.get("evidence", []) if isinstance(item, dict)}
    references = checks[0].get("evidence")
    return isinstance(references, list) and bool(references) and all(
        reference in receipts and receipts[reference].get("tool") == tool
        and receipts[reference].get("returncode") == 0 and not receipts[reference].get("termination")
        for reference in references)


def _published_candidates(config, identity, task):
    root = Path(config["state_dir"]) / "runs"
    found = []
    for candidate in root.glob("*/*/result.json"):
        try:
            result_path = evidence_path(root, candidate.relative_to(root).as_posix())
            result = read_json(result_path)
            execution = read_json(evidence_path(result_path.parent, "execution.json"))
            environment = result.get("environment", {})
            if (execution.get("phase") != "published" or result.get("conclusion") != "success"
                    or result.get("schema") != "triton-anchor-local-ci-result/v4"
                    or result.get("tested_sha") != identity["base_sha"]
                    or environment.get("profile_id") != identity["profile_id"]
                    or environment.get("llvm_revision") != identity["llvm_revision"]
                    or result.get("repository") != task.get("repository")
                    or result.get("task_id") != result_path.parent.parent.name
                    or result.get("run_id") != result_path.parent.name):
                continue
            found.append((str(result.get("completed_at", "")), result_path.parent, result))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A corrupt or incomplete run must not prevent other eligible baselines.
            continue
    return sorted(found, key=lambda item: (item[0], str(item[1])), reverse=True)


def _snapshot_bytes(run, result, tool):
    if not _successful_tool(result, tool):
        return None
    relative = "artifacts/" + tool + "/candidate.json"
    manifest = result.get("artifacts", {})
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return None
    entries = [entry for entry in manifest["files"] if isinstance(entry, dict) and entry.get("path") == relative]
    if len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry.get("sha256"), str) or not DIGEST.fullmatch(entry["sha256"]):
        return None
    source = evidence_path(run, relative)
    size = source.stat().st_size
    if size > MAX_BASELINE_BYTES or size != entry.get("size"):
        return None
    payload = source.read_bytes()
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
        return None
    # Do not infer source identity from values inside this benchmark JSON.
    return payload, entry["sha256"]


def prepare_baselines(config, profile, task, host_task, container_task):
    """Return a tool -> trusted baseline record mapping; absence means unverified.

    Explicit profile records reference container files and are checked again by
    the tool inside the worker. Historical records are copied only after checking
    the host result, published phase, successful receipt and frozen artifact hash.
    The caller makes the returned baseline directory root-owned and read-only.
    """
    if profile.get("triton_version") != "3.0":
        return {}
    identity = _identity(profile, task)
    if not SHA.fullmatch(identity["base_sha"]) or not SHA.fullmatch(identity["llvm_revision"]):
        raise ValueError("Performance baseline selection requires exact base and LLVM SHAs")
    if not _container_path(container_task):
        raise ValueError("Task container path must be a canonical absolute path")
    selected = {}
    explicit = profile.get("performance_baselines", {})
    if not isinstance(explicit, dict):
        raise ValueError("profile.performance_baselines must be a tool mapping")
    for tool in TOOLS:
        record = explicit.get(tool)
        if not _matches(record, identity):
            continue
        if (not _container_path(record.get("path")) or not isinstance(record.get("sha256"), str)
                or not DIGEST.fullmatch(record["sha256"])):
            raise ValueError("Explicit performance baseline has an invalid path or SHA-256")
        selected[tool] = {**identity, "path": record["path"], "sha256": record["sha256"]}
    if len(selected) == len(TOOLS):
        return selected
    destination = evidence_path(Path(host_task), "baselines")
    for _, run, result in _published_candidates(config, identity, task):
        for tool in TOOLS:
            if tool in selected:
                continue
            try:
                snapshot = _snapshot_bytes(run, result, tool)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if snapshot is None:
                continue
            payload, checksum = snapshot
            destination.mkdir(parents=True, exist_ok=True)
            target = evidence_path(Path(host_task), "baselines/" + tool + ".json")
            if target.exists():
                # Recovery may read a root-owned baseline created before the crash.
                # Do not require write permission or replace its immutable bytes.
                if target.read_bytes() != payload:
                    raise ValueError("Existing task baseline differs from the selected frozen snapshot")
            else:
                temporary = evidence_path(Path(host_task), "baselines/" + tool + ".json.tmp")
                temporary.write_bytes(payload)
                temporary.replace(target)
            selected[tool] = {**identity, "path": container_task + "/baselines/" + tool + ".json",
                              "sha256": checksum}
        if len(selected) == len(TOOLS):
            break
    return selected
