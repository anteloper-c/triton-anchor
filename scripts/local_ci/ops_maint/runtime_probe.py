"""Explicit Rootless Docker capability probe; ordinary preflight only reads proof."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path

from ops_maint.manager import EnvironmentManager, atomic_json
from ops_maint.dependency_mounts import (
    dependency_mounts,
    mount_arguments,
    verify_mounts,
)

PROBE_SCHEMA = "triton-anchor-rootless-runtime-probe/v1"
DEFAULT_IDENTITIES = {"task": 11001, "gid": 11001}
LIMIT_PROBE = """import json,pathlib
p=pathlib.Path('/sys/fs/cgroup')
print(json.dumps({k:(p/k).read_text().strip() for k in ('cpu.max','memory.max','pids.max')}))
"""
DEPENDENCY_PROBE = """
import errno,os,sys,tempfile
for root in sys.argv[1:]:
    for directory,dirs,files in os.walk(root):
        assert os.access(directory, os.R_OK | os.X_OK), directory
        for name in files:
            with open(os.path.join(directory,name),'rb') as handle: handle.read(1)
    try:
        fd,path=tempfile.mkstemp(prefix='.local-ci-readonly-probe-',dir=root)
    except OSError as exc:
        assert exc.errno in (errno.EROFS,errno.EACCES,errno.EPERM), str(exc)
    else:
        os.close(fd)
        os.unlink(path)
        raise RuntimeError('Dependency is writable: ' + root)
"""


def validate_runtime_config(config):
    runtime = config.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError(
            "runtime must be an explicit Rootless Docker configuration object"
        )
    endpoint, context = runtime.get("endpoint"), runtime.get("context")
    if runtime.get("kind") != "docker-rootless":
        raise ValueError("runtime.kind must explicitly be docker-rootless")
    if not isinstance(endpoint, str) or not re.fullmatch(
        r"unix:///run/user/[1-9][0-9]*/[A-Za-z0-9_.-]+\.sock", endpoint
    ):
        raise ValueError(
            "Configure the actual private Rootless socket under /run/user/<CI UID>; system Docker fallback is forbidden"
        )
    if not isinstance(context, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", context
    ):
        raise ValueError("Configure the actual Rootless Docker CLI context")
    resources = config.get("resources", {})
    if not isinstance(resources, dict):
        raise ValueError("resources must configure CPU, memory and PID budgets")
    cpus = resources.get("cpus")
    if (
        type(cpus) not in (int, float)
        or not 0 < cpus < 100000
        or not math.isfinite(cpus)
    ):
        raise ValueError("resources.cpus must be a finite positive number")
    if any(
        type(resources.get(name)) is not int or resources[name] <= 0
        for name in ("memory_bytes", "pids_limit")
    ):
        raise ValueError(
            "resources.memory_bytes and resources.pids_limit must be positive integers"
        )
    configured = config.get("identities", {})
    if not isinstance(configured, dict):
        raise ValueError("identities must be a container UID/GID configuration object")
    identities = {**DEFAULT_IDENTITIES, **configured}
    if any(
        type(value) is not int or not 0 < value < 65536 for value in identities.values()
    ):
        raise ValueError(
            "Container identities must be explicit non-root integer UIDs/GIDs"
        )
    if set(identities) != {"task", "gid"}:
        raise ValueError("Configure only one task UID and GID")
    return runtime, resources, identities


def docker(config, *args, timeout=30):
    runtime, _, _ = validate_runtime_config(config)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    result = subprocess.run(
        [config.get("docker_bin", "docker"), "--host", runtime["endpoint"], *args],
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("Rootless Docker operation failed: " + args[0])
    return result.stdout


def runtime_status(config):
    runtime, _, _ = validate_runtime_config(config)
    if os.geteuid() == 0:
        raise ValueError(
            "Rootless preflight/probe must run as the ordinary CI user without sudo"
        )
    socket = Path(runtime["endpoint"][7:])
    if socket.parent != Path("/run/user") / str(os.getuid()) or socket.is_symlink():
        raise ValueError("Rootless endpoint must belong to the current CI UID")
    info = socket.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Configured Rootless endpoint is not this CI user's socket")
    context = json.loads(docker(config, "context", "inspect", runtime["context"]))
    if (
        not isinstance(context, list)
        or len(context) != 1
        or context[0].get("Endpoints", {}).get("docker", {}).get("Host")
        != runtime["endpoint"]
    ):
        raise ValueError("Configured Docker context points at a different endpoint")
    info = json.loads(docker(config, "info", "--format", "{{json .}}"))
    if not any(
        "rootless" in str(value).split(",") or "name=rootless" in str(value).split(",")
        for value in info.get("SecurityOptions", [])
    ):
        raise ValueError("Configured endpoint is not a Rootless Docker daemon")
    if str(info.get("CgroupVersion")) != "2" or info.get("CgroupDriver") != "systemd":
        raise ValueError(
            "Effective limits require cgroup v2 and the systemd cgroup driver"
        )
    if any(
        info.get(name) is True
        for name in ("NoCpuCfsQuota", "NoCpuCfsPeriod", "NoMemoryLimit", "NoPidsLimit")
    ):
        raise ValueError("Rootless Docker reports unavailable resource controllers")
    if not info.get("ID"):
        raise ValueError("Docker daemon identity is unavailable")
    return {
        "daemon_id": info["ID"],
        "endpoint": runtime["endpoint"],
        "uid": os.getuid(),
        "cgroup_version": str(info["CgroupVersion"]),
        "cgroup_driver": info["CgroupDriver"],
        "kernel_version": info.get("KernelVersion", ""),
        "server_version": info.get("ServerVersion", ""),
    }


def config_digest(config):
    public = {
        key: config.get(key)
        for key in (
            "schema",
            "runtime",
            "resources",
            "identities",
            "profiles",
            "branch_profiles",
            "dependency_root",
            "control_root",
            "codex_bin",
            "container_python",
        )
    }
    root = Path(config["control_root"])
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if revision.returncode or not re.fullmatch(
        r"[a-f0-9]{40}", revision.stdout.strip()
    ):
        raise ValueError("Trusted control Git revision is unavailable")
    public["worker_revision_sha"] = revision.stdout.strip()
    return hashlib.sha256(
        json.dumps(
            public, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def active_images(config):
    health = EnvironmentManager(config, config["state_dir"]).health()
    images = {row["release_id"]: row for row in health.get("images", [])}
    selected = {}
    for branch in config.get("profiles", {}):
        release = images.get(health.get("active_images", {}).get(branch))
        if (
            not release
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", release.get("image_id", ""))
            or release.get("validated") is not True
            or release.get("state") not in {"ready", "active"}
            or release.get("target_branch") != branch
            or release.get("llvm_hash") != config["profiles"][branch].get("llvm_hash")
        ):
            raise ValueError(
                "Build and validate a trusted image release before probing: " + branch
            )
        selected[branch] = release["image_id"]
    return selected


def validate_limits(actual, resources):
    try:
        quota, period = actual["cpu.max"].split()
        cpu = int(quota) / int(period)
        memory, pids = int(actual["memory.max"]), int(actual["pids.max"])
        if (
            abs(cpu - resources["cpus"]) > 0.00001
            or memory != resources["memory_bytes"]
            or pids != resources["pids_limit"]
        ):
            raise ValueError("Mismatch")
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        raise ValueError(
            "Container CPU/memory/PID limits are not effectively enforced"
        ) from None


def proof_path(config):
    return Path(config["state_dir"]) / "ops_maint/runtime-probe.json"


def verify_probe(config, info):
    for profile in config.get("profiles", {}).values():
        dependency_mounts(config, profile, verify_content=True)
    path = proof_path(config)
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError(
            "Missing private runtime proof; build images then explicitly run preflight --probe-runtime"
        )
    proof = json.loads(path.read_text())
    expected_images = active_images(config)
    if (
        proof.get("schema") != PROBE_SCHEMA
        or proof.get("runtime") != info
        or proof.get("config_digest") != config_digest(config)
        or proof.get("images") != expected_images
    ):
        raise ValueError(
            "Runtime/image/resource configuration changed; repeat preflight --probe-runtime"
        )
    if proof.get("status") != "pass" or set(proof.get("limits", {})) != set(
        expected_images
    ):
        raise ValueError("Runtime resource proof is incomplete")
    for value in proof["limits"].values():
        validate_limits(value, config["resources"])
    return proof


def probe_runtime(config):
    """Explicitly create only trusted canaries, never a PR or service unit."""
    info = runtime_status(config)
    _, resources, identities = validate_runtime_config(config)
    images = active_images(config)
    proof = {
        "schema": PROBE_SCHEMA,
        "status": "running",
        "runtime": info,
        "config_digest": config_digest(config),
        "images": images,
        "resources": resources,
        "limits": {},
        "probed_at": time.time(),
    }
    # A failed re-probe must revoke an earlier passing proof for the same config.
    atomic_json(proof_path(config), proof)
    proof_path(config).chmod(0o600)
    try:
        for branch, image in images.items():
            mounts = dependency_mounts(
                config, config["profiles"][branch], verify_content=True
            )
            nonce = uuid.uuid4().hex
            name = "local-ci-deployment-probe-" + nonce
            container = ""
            try:
                container = docker(
                    config,
                    "create",
                    "--name",
                    name,
                    "--label",
                    "triton-anchor.role=deployment-probe",
                    "--label",
                    "triton-anchor.probe=" + nonce,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--cpus",
                    str(resources["cpus"]),
                    "--memory",
                    str(resources["memory_bytes"]),
                    "--pids-limit",
                    str(resources["pids_limit"]),
                    "--user",
                    str(identities["task"]),
                    "--entrypoint",
                    config.get("container_python", "python3"),
                    *mount_arguments(mounts),
                    image,
                    "-I",
                    "-c",
                    LIMIT_PROBE + DEPENDENCY_PROBE,
                    *(entry["target"] for entry in mounts),
                ).strip()
                if not re.fullmatch(r"[a-f0-9]{64}", container):
                    raise ValueError(
                        "Docker returned an invalid deployment probe identity"
                    )
                proof["limits"][branch] = json.loads(
                    docker(config, "start", "--attach", container, timeout=120)
                )
                validate_limits(proof["limits"][branch], resources)
                if mounts:
                    verify_mounts(
                        json.loads(docker(config, "inspect", container))[0], mounts
                    )
            finally:
                # Name+nonce also identifies a create that timed out before returning its ID.
                try:
                    inspection = json.loads(
                        docker(
                            config,
                            "inspect",
                            container
                            if re.fullmatch(r"[a-f0-9]{64}", container)
                            else name,
                        )
                    )
                except RuntimeError:
                    if container:
                        raise
                    inspection = []
                if inspection:
                    actual = inspection[0].get("Id", "")
                    if (
                        len(inspection) != 1
                        or not re.fullmatch(r"[a-f0-9]{64}", actual)
                        or (container and actual != container)
                        or inspection[0]
                        .get("Config", {})
                        .get("Labels", {})
                        .get("triton-anchor.probe")
                        != nonce
                        or inspection[0]
                        .get("Config", {})
                        .get("Labels", {})
                        .get("triton-anchor.role")
                        != "deployment-probe"
                    ):
                        raise ValueError(
                            "Probe ownership changed; refusing container cleanup"
                        )
                    docker(config, "rm", "--force", actual)
        if (
            config_digest(config) != proof["config_digest"]
            or active_images(config) != images
        ):
            raise ValueError(
                "Trusted control or image release changed during runtime probe"
            )
    except Exception as exc:
        proof.update(status="fail", failure=type(exc).__name__)
        atomic_json(proof_path(config), proof)
        proof_path(config).chmod(0o600)
        raise
    proof["status"] = "pass"
    atomic_json(proof_path(config), proof)
    proof_path(config).chmod(0o600)
    return proof
