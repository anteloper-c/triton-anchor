#!/usr/bin/env python3
"""Manage one Local CI task's owned temporary directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TASK_ROOT_PREFIX = "triton-anchor-local-ci-task."
TASK_ROOT_PATTERN = re.compile(
    rf"^{re.escape(TASK_ROOT_PREFIX)}([0-9a-f]{{12}})\.([A-Za-z0-9]{{6}})$"
)
MARKER_NAME = ".local-ci-task-owner.json"
MARKER_SCHEMA = "triton-anchor-local-ci-task-tmp/v1"
TASK_SUBDIRECTORIES = ("tmp", "dump", "credentials", "benchmark", "runner")


@dataclass(frozen=True)
class CleanupPlan:
    root: Path
    files: tuple[Path, ...]
    directories: tuple[Path, ...]
    bytes: int


def validate_target_sha(target_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
        raise ValueError("target SHA must be a lowercase 40-character hexadecimal commit")


def validate_task_root(
    path: Path,
    target_sha: str,
    *,
    parent: Path = Path("/tmp"),
    require_exists: bool = True,
) -> Path:
    validate_target_sha(target_sha)
    if not path.is_absolute():
        raise ValueError(f"task temporary root must be absolute: {path}")

    parent = parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"task temporary parent must be a real directory: {parent}")
    if path.parent.resolve(strict=True) != parent:
        raise ValueError(f"task temporary root must be an immediate child of {parent}: {path}")

    match = TASK_ROOT_PATTERN.fullmatch(path.name)
    if match is None or match.group(1) != target_sha[:12]:
        raise ValueError(f"invalid task temporary root name for {target_sha}: {path}")

    if not path.exists() and not path.is_symlink():
        if require_exists:
            raise ValueError(f"task temporary root does not exist: {path}")
        return path

    root_stat = path.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"task temporary root must be a real directory: {path}")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent or resolved.name != path.name:
        raise ValueError(f"task temporary root escaped its parent: {path} -> {resolved}")
    if os.path.ismount(resolved):
        raise ValueError(f"refusing to manage mounted task temporary root: {resolved}")
    return resolved


def marker_path(root: Path) -> Path:
    return root / MARKER_NAME


def read_marker(root: Path, target_sha: str) -> dict[str, object]:
    path = marker_path(root)
    marker_stat = path.lstat()
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        raise ValueError(f"task ownership marker must be a regular file: {path}")
    if marker_stat.st_size > 64 * 1024:
        raise ValueError(f"task ownership marker is unexpectedly large: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != MARKER_SCHEMA:
        raise ValueError(f"unexpected task ownership marker schema: {path}")
    if document.get("target_sha") != target_sha:
        raise ValueError(f"task ownership marker SHA mismatch: {path}")
    if document.get("task_root") != str(root):
        raise ValueError(f"task ownership marker path mismatch: {path}")
    return document


def prepare_task_root(
    path: Path,
    target_sha: str,
    *,
    parent: Path = Path("/tmp"),
) -> dict[str, object]:
    root = validate_task_root(path, target_sha, parent=parent)
    root.chmod(0o700)
    for name in TASK_SUBDIRECTORIES:
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)

    marker = marker_path(root)
    if marker.exists() or marker.is_symlink():
        document = read_marker(root, target_sha)
    else:
        document = {
            "schema": MARKER_SCHEMA,
            "target_sha": target_sha,
            "task_root": str(root),
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return document


def build_cleanup_plan(root: Path) -> CleanupPlan:
    root_stat = root.lstat()
    root_device = root_stat.st_dev
    files: list[Path] = []
    directories: list[Path] = []
    total_bytes = 0

    for current_text, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        traversed_directories: list[str] = []
        for name in sorted(directory_names):
            child = current / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                files.append(child)
                total_bytes += child_stat.st_size
                continue
            if not stat.S_ISDIR(child_stat.st_mode):
                raise ValueError(f"unexpected directory entry type: {child}")
            if child_stat.st_dev != root_device or os.path.ismount(child):
                raise ValueError(f"refusing to cross nested mount or filesystem: {child}")
            traversed_directories.append(name)
            directories.append(child)
        directory_names[:] = traversed_directories

        for name in sorted(file_names):
            child = current / name
            child_stat = child.lstat()
            if child_stat.st_dev != root_device and not stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(f"refusing to cross filesystem boundary: {child}")
            files.append(child)
            total_bytes += child_stat.st_size

    return CleanupPlan(
        root=root,
        files=tuple(files),
        directories=tuple(directories),
        bytes=total_bytes,
    )


def execute_cleanup_plan(plan: CleanupPlan) -> dict[str, int | str]:
    for child in plan.files:
        child.unlink()
    for child in reversed(plan.directories):
        child.rmdir()
    plan.root.rmdir()
    return {
        "task_root": str(plan.root),
        "removed_files": len(plan.files),
        "removed_directories": len(plan.directories) + 1,
        "removed_bytes": plan.bytes,
    }


def cleanup_owned_path(
    path: Path,
    target_sha: str,
    relative: str,
    *,
    parent: Path = Path("/tmp"),
) -> dict[str, int | str]:
    root = validate_task_root(path, target_sha, parent=parent)
    read_marker(root, target_sha)
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or relative_path.parts[0] != "dump"
        or relative_path.parts[1] in {"", ".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9._-]+", relative_path.parts[1])
    ):
        raise ValueError(f"owned cleanup path must be one dump stage: {relative!r}")

    target = root / relative_path
    if not target.exists() and not target.is_symlink():
        return {
            "task_root": str(target),
            "removed_files": 0,
            "removed_directories": 0,
            "removed_bytes": 0,
        }
    target_stat = target.lstat()
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        raise ValueError(f"owned cleanup target must be a real directory: {target}")
    if target_stat.st_dev != root.lstat().st_dev or os.path.ismount(target):
        raise ValueError(f"refusing to clean mounted owned path: {target}")
    return execute_cleanup_plan(build_cleanup_plan(target))


def cleanup_task_root(
    path: Path,
    target_sha: str,
    *,
    parent: Path = Path("/tmp"),
) -> dict[str, int | str]:
    root = validate_task_root(
        path,
        target_sha,
        parent=parent,
        require_exists=False,
    )
    if not root.exists() and not root.is_symlink():
        return {
            "task_root": str(root),
            "removed_files": 0,
            "removed_directories": 0,
            "removed_bytes": 0,
        }

    root = validate_task_root(root, target_sha, parent=parent)
    read_marker(root, target_sha)
    return execute_cleanup_plan(build_cleanup_plan(root))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "validate", "cleanup", "clean-owned"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--path", required=True)
        command_parser.add_argument("--target-sha", required=True)
        if command == "clean-owned":
            command_parser.add_argument("--relative", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.path)
    if args.command == "prepare":
        result = prepare_task_root(path, args.target_sha)
    elif args.command == "validate":
        root = validate_task_root(path, args.target_sha)
        result = read_marker(root, args.target_sha)
    elif args.command == "cleanup":
        result = cleanup_task_root(path, args.target_sha)
    else:
        result = cleanup_owned_path(path, args.target_sha, args.relative)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
