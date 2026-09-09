#!/usr/bin/env python3
"""Create admitted task metadata from frozen runner inputs, never from candidate code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


def build_metadata(values: dict, pull: dict | None = None) -> dict:
    pull = pull or {}
    repository = values["REPOSITORY"]
    if repository != "anteloper-c/triton-anchor":
        raise ValueError("Only the authorized target repository may dispatch tasks")
    number = int(values.get("PR_NUMBER") or 0)
    is_pr = number > 0
    result = {
        "schema": "triton-anchor-local-ci-task-metadata",
        "repository": repository, "event_kind": "pull_request" if is_pr else "push",
        "execution_mode": "ai", "pr_number": number,
        "task_ref": values["TASK_REF"], "base_task_ref": values.get("BASE_TASK_REF", ""),
        "head_task_ref": values.get("HEAD_TASK_REF", ""),
        "target_sha": values["TESTED_SHA"], "tested_sha": values["TESTED_SHA"],
        "tested_ref": values["TESTED_REF"], "tested_sha_kind": "pr_merge" if is_pr else "commit",
        "worker_revision_sha": values["WORKER_REVISION_SHA"],
        "base_branch": values.get("BASE_REF") or values["TARGET_BRANCH"], "base_sha": values.get("BASE_SHA") or values["TESTED_SHA"],
        "head_branch": values["HEAD_REF"], "head_sha": values["HEAD_SHA"],
        "head_repo": values["HEAD_REPO"], "target_branch": values["TARGET_BRANCH"],
        "captured_at": values["CAPTURED_AT"],
        "flaggems_mode": values.get("FLAGGEMS_MODE", "sample"),
        "triton_version": values["TRITON_VERSION"],
        "changed_paths": json.loads(values.get("CHANGED_PATHS_JSON", "[]")),
    }
    if not re.fullmatch(r"3\.\d+", result["triton_version"]):
        raise ValueError("The tested source has no supported Triton version declaration")
    if not isinstance(result["changed_paths"], list) or any(not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts for path in result["changed_paths"]):
        raise ValueError("Invalid changed file list")
    for name in ("tested_sha", "head_sha", "worker_revision_sha") + (("base_sha",) if is_pr else ()):
        if not re.fullmatch(r"[a-f0-9]{40}", result[name]):
            raise ValueError(f"Invalid frozen SHA: {name}")
    if result["flaggems_mode"] not in {"sample", "full"}:
        raise ValueError("Unknown FlagGems selection")
    title, description = str(pull.get("title") or ""), str(pull.get("body") or "")
    result.update(title=title[:500], description=description[:8000], title_truncated=len(title) > 500,
                  description_truncated=len(description) > 8000,
                  labels=[str(item["name"]) for item in pull.get("labels", []) if isinstance(item, dict) and isinstance(item.get("name"), str)])
    if is_pr:
        if (pull.get("number") != number or pull.get("state") != "open" or pull.get("draft") or
                pull.get("head", {}).get("sha") != result["head_sha"] or
                pull.get("head", {}).get("ref") != result["head_branch"] or
                pull.get("head", {}).get("repo", {}).get("full_name") != result["head_repo"] or
                pull.get("base", {}).get("sha") != result["base_sha"] or
                pull.get("base", {}).get("ref") != result["target_branch"]):
            raise ValueError("PR changed after preflight or approval")
        if values.get("PREFLIGHT_PASSED") != "true":
            raise ValueError("PR preflight checks did not all pass")
    external = is_pr and result["head_repo"] != repository
    if external and values.get("EXTERNAL_APPROVAL") != "true":
        raise ValueError("External PR requires explicit environment approval for this task")
    result["preflight"] = {name: "success" if is_pr else "not_applicable" for name in ("pr_information", "basic", "api", "security")}
    result["preflight"].update(run_id=values["RUN_ID"], run_attempt=values["RUN_ATTEMPT"],
                               url=f"https://github.com/{repository}/actions/runs/{values['RUN_ID']}")
    if not is_pr:
        result["preflight"]["reason"] = "maintainer branch/full task; no PR preflight"
    result["approval"] = {"required": external, "status": "approved" if external else "not_required"}
    result["approval"].update({key: result[key] for key in ("head_sha", "base_sha", "tested_sha", "worker_revision_sha")})
    identity = {key: result[key] for key in ("repository", "task_ref", "tested_sha", "head_sha", "base_sha", "worker_revision_sha")}
    identity.update(dispatch_id=f"{values['RUN_ID']}-{values['RUN_ATTEMPT']}")
    result["task_id"] = "task-" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pull = json.loads(args.pull_json.read_text(encoding="utf-8")) if args.pull_json else None
    values = dict(os.environ)
    declaration = Path("triton/python/triton/__init__.py").read_text(encoding="utf-8")
    match = re.search(r"(?m)^__version__\s*=\s*['\"](3\.\d+)\.", declaration)
    if not match:
        raise ValueError("Cannot identify Triton version in the frozen checkout")
    values["TRITON_VERSION"] = match[1]
    paths = []
    if values.get("PR_NUMBER"):
        raw = subprocess.check_output(["git", "diff", "--name-only", "-z", "--no-renames", f"{values['BASE_SHA']}...{values['HEAD_SHA']}"])
        paths = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    values["CHANGED_PATHS_JSON"] = json.dumps(paths)
    metadata = build_metadata(values, pull)
    args.output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"task_id={metadata['task_id']}\n")


if __name__ == "__main__":
    main()
