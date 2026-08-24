"""Read Wheel evidence without importing or extracting artifact code."""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import re
import stat
import sys
import tokenize
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class WheelValidationError(ValueError):
    """Raised when a Wheel cannot be used as trustworthy artifact evidence."""


_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_NATIVE_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
_SIGNATURE_SUFFIXES = (".jws", ".p7s")


def _safe_member_name(name: str) -> str:
    if not name or "\x00" in name:
        raise WheelValidationError("Wheel contains an empty or NUL-containing path")
    if "\\" in name:
        raise WheelValidationError(f"Wheel path must use '/': {name!r}")
    if name.startswith("/") or name.startswith("//") or _DRIVE_PATH.match(name):
        raise WheelValidationError(f"Wheel contains an absolute path: {name!r}")

    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts):
        raise WheelValidationError(f"Wheel contains a non-normal path: {name!r}")
    normalized = path.as_posix()
    if normalized != name.rstrip("/"):
        raise WheelValidationError(f"Wheel contains a non-normal path: {name!r}")
    return normalized


def _digest_stream(stream: BinaryIO, algorithm: str) -> bytes:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise WheelValidationError(f"RECORD uses unsupported hash {algorithm!r}") from exc
    if digest.digest_size < hashlib.sha256().digest_size:
        raise WheelValidationError(f"RECORD uses weak hash {algorithm!r}")
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.digest()


def _decode_record_digest(value: str) -> tuple[str, bytes]:
    if "=" not in value:
        raise WheelValidationError(f"Malformed RECORD digest: {value!r}")
    algorithm, encoded = value.split("=", 1)
    if not algorithm or not encoded:
        raise WheelValidationError(f"Malformed RECORD digest: {value!r}")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise WheelValidationError(f"Malformed RECORD digest: {value!r}") from exc
    return algorithm.lower(), raw


def _wheel_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_native(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(_NATIVE_SUFFIXES) or ".so." in lowered


def _python_import_inventory(
    archive: zipfile.ZipFile, files: dict[str, zipfile.ZipInfo]
) -> tuple[list[dict[str, object]], list[str]]:
    python_paths = sorted(name for name in files if name.endswith(".py"))
    package_roots = {
        PurePosixPath(name).parts[0]
        for name in python_paths
        if PurePosixPath(name).parts
        and not PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    standard_library = set(getattr(sys, "stdlib_module_names", ()))
    standard_library.update(sys.builtin_module_names)
    standard_library.add("__future__")
    paths_by_import: dict[str, set[str]] = {}
    issues: list[str] = []
    for name in python_paths:
        data = archive.read(name)
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            source = data.decode(encoding)
            tree = ast.parse(source, filename=name)
        except (LookupError, SyntaxError, UnicodeError) as exc:
            issues.append(f"cannot parse Python imports from {name}: {exc}")
            continue
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append(node.module.split(".", 1)[0])
            for root in imported:
                if root in standard_library or root in package_roots:
                    continue
                paths_by_import.setdefault(root, set()).add(name)
    inventory = [
        {
            "name": name,
            "paths": sorted(paths),
            "context": "test-only"
            if all("tests" in PurePosixPath(path).parts for path in paths)
            else "runtime-external",
        }
        for name, paths in sorted(paths_by_import.items())
    ]
    return inventory, issues


def _filename_tags(filename: str) -> dict[str, str]:
    if not filename.endswith(".whl"):
        raise WheelValidationError(f"Candidate is not a .whl file: {filename}")
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        raise WheelValidationError(f"Malformed Wheel filename: {filename}")
    return {
        "python_tag": parts[-3],
        "abi_tag": parts[-2],
        "platform_tag": parts[-1],
    }


def _read_archive(path: Path) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelValidationError(f"Cannot read Wheel archive: {exc}") from exc

    files: dict[str, zipfile.ZipInfo] = {}
    folded: dict[str, str] = {}
    try:
        for info in archive.infolist():
            name = _safe_member_name(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise WheelValidationError(f"Wheel contains a symbolic link: {name}")
            if info.is_dir():
                continue
            key = name.casefold()
            if key in folded:
                raise WheelValidationError(
                    f"Wheel contains colliding paths: {folded[key]!r} and {name!r}"
                )
            folded[key] = name
            files[name] = info
    except Exception:
        archive.close()
        raise
    return archive, files


def _validate_record_in_archive(
    archive: zipfile.ZipFile, files: dict[str, zipfile.ZipInfo]
) -> dict[str, object]:
    record_paths = [name for name in files if name.endswith(".dist-info/RECORD")]
    if len(record_paths) != 1:
        raise WheelValidationError(
            f"Wheel must contain exactly one .dist-info/RECORD; found {len(record_paths)}"
        )
    record_path = record_paths[0]
    try:
        record_text = archive.read(record_path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelValidationError("RECORD is not UTF-8") from exc

    rows: dict[str, tuple[str, str]] = {}
    folded: dict[str, str] = {}
    try:
        reader = csv.reader(io.StringIO(record_text, newline=""))
        for row_number, row in enumerate(reader, 1):
            if len(row) != 3:
                raise WheelValidationError(
                    f"RECORD row {row_number} must contain exactly three fields"
                )
            member = _safe_member_name(row[0])
            key = member.casefold()
            if key in folded:
                raise WheelValidationError(
                    f"RECORD contains duplicate paths: {folded[key]!r} and {member!r}"
                )
            folded[key] = member
            rows[member] = (row[1], row[2])
    except csv.Error as exc:
        raise WheelValidationError(f"Cannot parse RECORD: {exc}") from exc

    signatures = {
        f"{record_path}{suffix}" for suffix in _SIGNATURE_SUFFIXES if f"{record_path}{suffix}" in files
    }
    missing = sorted((set(files) - signatures) - set(rows))
    extra = sorted(set(rows) - set(files))
    if missing or extra:
        raise WheelValidationError(
            f"RECORD/archive mismatch; missing={missing!r}, extra={extra!r}"
        )

    verified = 0
    for member, (digest_text, size_text) in rows.items():
        if member == record_path or member in signatures:
            if digest_text:
                algorithm, expected = _decode_record_digest(digest_text)
                with archive.open(member) as stream:
                    actual = _digest_stream(stream, algorithm)
                if actual != expected:
                    raise WheelValidationError(f"RECORD hash mismatch for {member}")
            if size_text and int(size_text) != files[member].file_size:
                raise WheelValidationError(f"RECORD size mismatch for {member}")
            continue
        if not digest_text or not size_text:
            raise WheelValidationError(f"RECORD omits hash or size for {member}")
        try:
            expected_size = int(size_text)
        except ValueError as exc:
            raise WheelValidationError(f"RECORD has invalid size for {member}") from exc
        if expected_size != files[member].file_size:
            raise WheelValidationError(f"RECORD size mismatch for {member}")
        algorithm, expected = _decode_record_digest(digest_text)
        with archive.open(member) as stream:
            actual = _digest_stream(stream, algorithm)
        if actual != expected:
            raise WheelValidationError(f"RECORD hash mismatch for {member}")
        verified += 1

    return {
        "path": record_path,
        "entry_count": len(rows),
        "verified_entry_count": verified,
        "signature_files": sorted(signatures),
        "status": "pass",
    }


def inspect_wheel(path: str | Path) -> dict[str, object]:
    """Return artifact identity and file evidence without importing its code."""

    wheel_path = Path(path).resolve()
    if not wheel_path.is_file():
        raise WheelValidationError(f"Wheel does not exist: {wheel_path}")
    tags = _filename_tags(wheel_path.name)
    archive, files = _read_archive(wheel_path)
    try:
        record = _validate_record_in_archive(archive, files)
        metadata_paths = [name for name in files if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise WheelValidationError(
                f"Wheel must contain exactly one .dist-info/METADATA; found {len(metadata_paths)}"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        project_name = metadata.get("Name")
        version = metadata.get("Version")
        if not project_name or not version:
            raise WheelValidationError("Wheel METADATA must contain Name and Version")

        python_imports, python_import_issues = _python_import_inventory(archive, files)

        native_files: list[dict[str, object]] = []
        file_manifest: list[dict[str, object]] = []
        for name in sorted(files):
            info = files[name]
            entry: dict[str, object] = {"path": name, "size": info.file_size}
            if _is_native(name):
                with archive.open(name) as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                entry["sha256"] = digest.hexdigest()
                native_files.append(dict(entry))
            file_manifest.append(entry)
    finally:
        archive.close()

    return {
        "schema_version": 1,
        "artifact_kind": "wheel",
        "filename": wheel_path.name,
        "name": project_name,
        "version": version,
        **tags,
        "sha256": _wheel_sha256(wheel_path),
        "size": wheel_path.stat().st_size,
        "record": record,
        "files": file_manifest,
        "native_files": native_files,
        "python_imports": python_imports,
        "python_import_issues": python_import_issues,
    }
