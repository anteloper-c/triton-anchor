"""Select LLVM from frozen source objects without trusting candidate recipes."""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path


SHA = re.compile(r"[0-9a-f]{40}")
LLVM_PATH = "triton/cmake/llvm-hash.txt"


class EnvironmentSelectionError(ValueError):
    pass


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="strict", timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise EnvironmentSelectionError("Cannot read the frozen LLVM dependency object")
    return result.stdout


def _git(repo, *args):
    return _run(["git", "-C", str(repo), "-c", "core.hooksPath=" + str(Path(repo) / "no-hooks"), *args])


def _entry(repo, revision, path):
    listing = _git(repo, "ls-tree", revision, "--", path).strip()
    if not listing:
        return None
    if "\n" in listing:
        raise EnvironmentSelectionError("Ambiguous frozen dependency path")
    fields, name = listing.split("\t", 1)
    mode, kind, object_id = fields.split(" ")
    if name != path or not SHA.fullmatch(object_id):
        raise EnvironmentSelectionError("Invalid frozen dependency entry")
    return mode, kind, object_id


def _requested_revision(config, relay, task, profile):
    tested = task.get("tested_sha", "")
    if not isinstance(tested, str) or not SHA.fullmatch(tested):
        raise EnvironmentSelectionError("Invalid frozen tested SHA")
    entry = _entry(relay.mirror, tested, LLVM_PATH)
    if entry:
        if entry[:2] != ("100644", "blob"):
            raise EnvironmentSelectionError("LLVM revision must be a regular tracked file")
        return _git(relay.mirror, "cat-file", "blob", entry[2]).strip(), tested, "source_blob"
    triton = _entry(relay.mirror, tested, "triton")
    if not triton:
        return None, None, "profile"
    if triton[:2] != ("160000", "commit"):
        raise EnvironmentSelectionError("Frozen Triton source has no LLVM revision file")
    source_sha = triton[2]
    host_source = profile.get("dependency_host_sources", {}).get("triton")
    if host_source:
        source = Path(host_source)
        if not source.is_absolute():
            raise EnvironmentSelectionError("Trusted dependency host source must be absolute")
        entry = _entry(source, source_sha, "cmake/llvm-hash.txt")
        if not entry or entry[:2] != ("100644", "blob"):
            raise EnvironmentSelectionError("Frozen Triton gitlink has no regular LLVM revision file")
        return _git(source, "cat-file", "blob", entry[2]).strip(), source_sha, "trusted_host_gitlink"
    container_source = profile.get("dependency_sources", {}).get("triton")
    if not isinstance(container_source, str) or not container_source.startswith("/"):
        raise EnvironmentSelectionError("Frozen Triton gitlink requires a trusted dependency source")
    # Only an existing fixed worker may be inspected; this module never creates a container.
    prefix = [config.get("docker", "docker"), "exec", "--user", "0:0",
              profile["container"]["name"], "git", "-c", "core.hooksPath=/dev/null",
              "-c", "safe.directory=" + container_source, "-C", container_source]
    listing = _run(prefix + ["ls-tree", source_sha, "--", "cmake/llvm-hash.txt"]).strip()
    if not re.fullmatch(r"100644 blob [0-9a-f]{40}\tcmake/llvm-hash\.txt", listing):
        raise EnvironmentSelectionError("Frozen Triton gitlink has no regular LLVM revision file")
    object_id = listing.split("\t", 1)[0].split(" ")[2]
    return _run(prefix + ["cat-file", "blob", object_id]).strip(), source_sha, "trusted_container_gitlink"


def _require_recipe(config, profile):
    recipe = profile.get("maintenance", {}).get("recipe")
    if not isinstance(recipe, dict) or not recipe.get("context"):
        raise EnvironmentSelectionError("LLVM changed but no trusted maintenance recipe is configured")
    context = Path(recipe["context"])
    if not context.is_absolute():
        raise EnvironmentSelectionError("Trusted recipe context must be an absolute host path")
    context = context.resolve(strict=True)
    workspace = config.get("workspace_host")
    if workspace and context.is_relative_to(Path(workspace).resolve()):
        raise EnvironmentSelectionError("Trusted LLVM recipe cannot be inside the task workspace")
    dockerfile = (context / recipe.get("dockerfile", "Dockerfile")).resolve(strict=True)
    script = (context / "prepare_llvm.sh").resolve(strict=True)
    if not dockerfile.is_relative_to(context) or not script.is_relative_to(context):
        raise EnvironmentSelectionError("Trusted LLVM recipe files must remain within its context")
    if not dockerfile.is_file() or not script.is_file():
        raise EnvironmentSelectionError("Trusted LLVM recipe needs its Dockerfile and prepare_llvm.sh")


def resolve_profile(config, relay, task, profile):
    """Return an independent profile selected solely from exact Git objects.

    The caller must ensure/rebuild the fixed worker, then persist the selected
    revision for maintenance. Selection itself performs no mutation or fetching.
    For a gitlink, provide a host mirror for first startup or an already-running
    fixed worker containing the trusted dependency source. Never use its HEAD.
    """
    selected = copy.deepcopy(profile)
    requested, source_sha, source = _requested_revision(config, relay, task, profile)
    original = profile.get("llvm_revision", "")
    if not isinstance(original, str) or not SHA.fullmatch(original):
        raise EnvironmentSelectionError("Profile LLVM revision must be 40 lowercase hexadecimal characters")
    requested = original if requested is None else requested
    if not SHA.fullmatch(requested):
        raise EnvironmentSelectionError("Frozen LLVM revision must be 40 lowercase hexadecimal characters")
    changed = requested != original
    if changed:
        _require_recipe(config, profile)
    selected["llvm_revision"] = requested
    selected["llvm_selection"] = {"source": source, "source_sha": source_sha,
                                  "tested_sha": task["tested_sha"], "configured_revision": original,
                                  "requested_revision": requested, "rebuild_required": changed}
    return selected
