"""Committed control snapshots, isolated from the mutable deployment checkout."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile

from .artifacts import (
    EnvironmentError,
    SHA_RE,
    atomic_json,
    extract_verified_archive,
    file_digest,
    tree_digest,
)

CONTROL_TARGET = "/opt/local-ci/control"


def verify_snapshot(snapshot):
    root = Path(snapshot["source"])
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or tree_digest(root) != snapshot["sha256"]
    ):
        raise EnvironmentError("Control snapshot content or location changed")


def snapshot_control(control_root, revision, directory, run):
    if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        raise EnvironmentError("Control snapshot requires an exact committed revision")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory or any(c in str(directory) for c in ",\n\r"):
        raise EnvironmentError("Unsafe control snapshot directory")
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root, manifest = directory / revision, directory / (revision + ".json")
        if root.exists() or manifest.exists():
            try:
                snapshot = json.loads(manifest.read_text())
                if snapshot["source"] != str(root) or snapshot["revision"] != revision:
                    raise ValueError("Snapshot identity differs")
                verify_snapshot(snapshot)
                return snapshot
            except (OSError, ValueError, KeyError) as exc:
                raise EnvironmentError(
                    "Control snapshot cache is incomplete or changed"
                ) from exc
        with tempfile.TemporaryDirectory(
            prefix=".snapshot-", dir=directory
        ) as temporary:
            temporary = Path(temporary)
            archive = temporary / "control.tar"
            # Only tracked runtime inputs; never .git, deployment secrets or dirty files.
            paths = (
                run(
                    [
                        "git",
                        "-C",
                        str(control_root),
                        "ls-tree",
                        "--name-only",
                        revision,
                        "--",
                        "scripts",
                        "api_contract",
                        "envsetup.sh",
                    ]
                )
                .decode()
                .splitlines()
            )
            if "scripts" not in paths:
                raise EnvironmentError("Committed control scripts are missing")
            run(
                [
                    "git",
                    "-C",
                    str(control_root),
                    "archive",
                    "--format=tar",
                    "--output=" + str(archive),
                    revision,
                    "--",
                    *paths,
                ]
            )
            extracted = temporary / "tree"
            extract_verified_archive(archive, extracted, file_digest(archive), 0)
            for path in [extracted, *extracted.rglob("*")]:
                if not path.is_symlink():
                    path.chmod(
                        0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644
                    )
            digest = tree_digest(extracted)
            os.replace(extracted, root)
            snapshot = dict(source=str(root), revision=revision, sha256=digest)
            atomic_json(manifest, snapshot)
            return snapshot


def mount_arguments(snapshot):
    return [
        "--mount",
        "type=bind,source="
        + snapshot["source"]
        + ",target="
        + CONTROL_TARGET
        + ",readonly,bind-recursive=disabled",
    ]


def verify_mount(info, snapshot):
    mounts = [
        m for m in info.get("Mounts", []) if m.get("Destination") == CONTROL_TARGET
    ]
    if (
        len(mounts) != 1
        or mounts[0].get("Type") != "bind"
        or mounts[0].get("Source") != snapshot["source"]
        or mounts[0].get("RW") is not False
    ):
        raise EnvironmentError(
            "Control snapshot mount identity or readonly mode changed"
        )
