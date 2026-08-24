"""Inspect GitHub-generated source archives without executing their contents."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .model import stable_json, write_json


_COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_REFERENCE_KINDS = {"commit", "tag"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class SourceSnapshotValidationError(ValueError):
    """Raised when two archives do not identify one GitHub source snapshot."""


def _normalized_path(name: str) -> tuple[str, str]:
    if not name or "\\" in name or "\x00" in name:
        raise SourceSnapshotValidationError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        path.is_absolute()
        or len(parts) < 2
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SourceSnapshotValidationError(f"unsafe archive member path: {name!r}")
    return parts[0], PurePosixPath(*parts[1:]).as_posix()


def _entry(path: str, data: bytes, *, symlink: bool, executable: bool) -> dict[str, Any]:
    return {
        "path": path,
        "type": "symlink" if symlink else "file",
        "mode": "120000" if symlink else ("100755" if executable else "100644"),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _zip_manifest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    roots: set[str] = set()
    entries: dict[str, dict[str, Any]] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                root, relative = _normalized_path(info.filename)
                roots.add(root)
                if relative in entries:
                    raise SourceSnapshotValidationError(
                        f"duplicate archive member after root normalization: {relative}"
                    )
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                symlink = stat.S_ISLNK(unix_mode)
                data = archive.read(info)
                entries[relative] = _entry(
                    relative,
                    data,
                    symlink=symlink,
                    executable=bool(unix_mode & 0o111),
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SourceSnapshotValidationError(f"cannot read source ZIP: {exc}") from exc
    if len(roots) != 1 or not entries:
        raise SourceSnapshotValidationError(
            "source ZIP must contain one top-level directory and at least one file"
        )
    return next(iter(roots)), [entries[name] for name in sorted(entries)]


def _tar_manifest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    roots: set[str] = set()
    entries: dict[str, dict[str, Any]] = {}
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                root, relative = _normalized_path(member.name)
                roots.add(root)
                if relative in entries:
                    raise SourceSnapshotValidationError(
                        f"duplicate archive member after root normalization: {relative}"
                    )
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise SourceSnapshotValidationError(
                            f"cannot read source archive member: {relative}"
                        )
                    data = stream.read()
                    symlink = False
                elif member.issym():
                    data = member.linkname.encode("utf-8", errors="surrogateescape")
                    symlink = True
                else:
                    raise SourceSnapshotValidationError(
                        f"unsupported source archive member type: {relative}"
                    )
                entries[relative] = _entry(
                    relative,
                    data,
                    symlink=symlink,
                    executable=bool(member.mode & 0o111),
                )
    except (OSError, tarfile.TarError) as exc:
        raise SourceSnapshotValidationError(f"cannot read source tar archive: {exc}") from exc
    if len(roots) != 1 or not entries:
        raise SourceSnapshotValidationError(
            "source tar archive must contain one top-level directory and at least one file"
        )
    return next(iter(roots)), [entries[name] for name in sorted(entries)]


def _file_identity(path: Path, archive_format: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise SourceSnapshotValidationError(f"cannot hash {path.name}: {exc}") from exc
    return {
        "filename": path.name,
        "format": archive_format,
        "sha256": digest.hexdigest(),
        "size": size,
    }


def _tree_sha256(files: list[dict[str, Any]], gitlinks: list[dict[str, str]]) -> str:
    manifest = {"schema_version": 1, "files": files, "gitlinks": gitlinks}
    return hashlib.sha256(stable_json(manifest).encode("utf-8")).hexdigest()


def _repository_gitlinks(
    repository_root: Path, commit_sha: str
) -> list[dict[str, str]]:
    try:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                commit_sha,
            ],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceSnapshotValidationError(
            f"cannot read Git tree for commit {commit_sha}"
        ) from exc

    gitlinks: list[dict[str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = header.split(b" ", 2)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSnapshotValidationError("cannot parse Git tree entry") from exc
        if mode != b"160000":
            continue
        normalized = PurePosixPath(path)
        if (
            normalized.is_absolute()
            or not normalized.parts
            or any(part in {"", ".", ".."} for part in normalized.parts)
        ):
            raise SourceSnapshotValidationError(f"unsafe gitlink path: {path!r}")
        gitlinks.append(
            {
                "path": normalized.as_posix(),
                "mode": mode.decode("ascii"),
                "object_type": object_type.decode("ascii"),
                "commit": object_id.decode("ascii").casefold(),
            }
        )
    return sorted(gitlinks, key=lambda item: item["path"])


def _verify_repository_snapshot(
    repository_root: Path,
    *,
    reference_kind: str,
    reference: str,
    commit_sha: str,
    files: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    revision = (
        f"refs/tags/{reference}^{{commit}}"
        if reference_kind == "tag"
        else f"{commit_sha}^{{commit}}"
    )
    try:
        resolved = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--verify", revision],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceSnapshotValidationError(
            f"cannot resolve {reference_kind} {reference!r} in the supplied Git repository"
        ) from exc
    if resolved.casefold() != commit_sha.casefold():
        raise SourceSnapshotValidationError(
            f"{reference_kind} {reference!r} resolves to {resolved}, not {commit_sha}"
        )

    with tempfile.TemporaryDirectory(prefix="triton-anchor-source-snapshot-") as temporary:
        archive_path = Path(temporary) / "repository.tar.gz"
        try:
            with archive_path.open("wb") as stream:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository_root),
                        "archive",
                        "--format=tar.gz",
                        "--prefix=snapshot/",
                        commit_sha,
                    ],
                    check=True,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SourceSnapshotValidationError(
                f"cannot materialize Git tree for commit {commit_sha}"
            ) from exc
        _, repository_files = _tar_manifest(archive_path)
    if repository_files != files:
        raise SourceSnapshotValidationError(
            "GitHub source archives do not match the declared Git commit tree"
        )
    binding = "verified-tag-commit" if reference_kind == "tag" else "verified-commit"
    return binding, _repository_gitlinks(repository_root, commit_sha)


def inspect_source_snapshot(
    source_zip: str | Path,
    source_tar: str | Path,
    *,
    repository: str,
    reference_kind: str,
    reference: str,
    commit_sha: str,
    version: str,
    repository_root: str | Path | None = None,
) -> dict[str, Any]:
    """Identify one logical snapshot represented by GitHub ZIP and tar.gz files."""

    if reference_kind not in _REFERENCE_KINDS:
        raise SourceSnapshotValidationError(
            f"reference_kind must be one of {sorted(_REFERENCE_KINDS)}"
        )
    if not repository or not reference or not version:
        raise SourceSnapshotValidationError(
            "repository, source reference, and version must be non-empty"
        )
    if not _COMMIT_SHA.fullmatch(commit_sha):
        raise SourceSnapshotValidationError("source commit must be a full Git object id")
    if reference_kind == "commit" and reference.casefold() != commit_sha.casefold():
        raise SourceSnapshotValidationError(
            "a commit snapshot reference must equal the declared source commit"
        )

    zip_path = Path(source_zip)
    tar_path = Path(source_tar)
    _, zip_files = _zip_manifest(zip_path)
    _, tar_files = _tar_manifest(tar_path)
    if zip_files != tar_files:
        raise SourceSnapshotValidationError(
            "GitHub source ZIP and tar.gz do not contain the same normalized tree"
        )

    binding = "unverified"
    gitlinks: list[dict[str, str]] = []
    if repository_root is not None:
        binding, gitlinks = _verify_repository_snapshot(
            Path(repository_root),
            reference_kind=reference_kind,
            reference=reference,
            commit_sha=commit_sha,
            files=zip_files,
        )

    archive_identities = sorted(
        [
            _file_identity(zip_path, "zip"),
            _file_identity(tar_path, "tar.gz"),
        ],
        key=lambda item: str(item["format"]),
    )
    repository_name = repository.rstrip("/").rsplit("/", 1)[-1]
    logical_name = _SAFE_NAME.sub(
        "-", f"{repository_name}-{reference}-source-snapshot"
    ).strip("-")
    tree_sha256 = _tree_sha256(zip_files, gitlinks)
    return {
        "schema_version": 1,
        "artifact_kind": "github-source-snapshot",
        "filename": logical_name,
        "name": repository_name,
        "version": version,
        "sha256": tree_sha256,
        "hash_scope": (
            "normalized-git-tree"
            if binding in {"verified-commit", "verified-tag-commit"}
            else "normalized-archive-content-tree"
        ),
        "repository": repository,
        "reference_kind": reference_kind,
        "source_reference": reference,
        "source_commit": commit_sha.casefold(),
        "source_identity_binding": binding,
        "representations": archive_identities,
        "files": zip_files,
        "gitlinks": gitlinks,
    }


def source_snapshot_discoveries(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Expose normalized source files to the shared component reconciler."""

    return {
        "source": "source-snapshot",
        "status": "success",
        "issues": [],
        "items": [
            {
                "kind": "source-snapshot-file",
                "path": member["path"],
                "size": member["size"],
                "sha256": member["sha256"],
                "source": "source-snapshot",
            }
            for member in artifact.get("files", [])
        ]
        + [
            {
                "kind": "dependency-declaration",
                "name": PurePosixPath(str(gitlink["path"])).name,
                "path": gitlink["path"],
                "version": gitlink["commit"],
                "candidate_usages": ["test-only"],
                "declaration_source": "gitlink",
                "source": "source-snapshot",
            }
            for gitlink in artifact.get("gitlinks", [])
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", required=True)
    parser.add_argument("--source-tar", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--reference-kind", choices=sorted(_REFERENCE_KINDS), required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository-root")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifact = inspect_source_snapshot(
            args.source_zip,
            args.source_tar,
            repository=args.repository,
            reference_kind=args.reference_kind,
            reference=args.reference,
            commit_sha=args.commit,
            version=args.version,
            repository_root=args.repository_root,
        )
        write_json(args.output, artifact)
        return 0
    except (OSError, SourceSnapshotValidationError) as exc:
        parser = build_parser()
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
