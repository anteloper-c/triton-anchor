from __future__ import annotations

import base64
import hashlib
import stat
import zipfile
from pathlib import Path


DIST_INFO = "demo-1.0.dist-info"
RECORD_PATH = f"{DIST_INFO}/RECORD"


def _hash_field(data: bytes, algorithm: str) -> str:
    digest = hashlib.new(algorithm, data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{algorithm}={encoded}"


def make_wheel(
    directory: Path,
    *,
    filename: str = "demo-1.0-py3-none-any.whl",
    members: list[tuple[str, bytes]] | None = None,
    algorithm: str = "sha256",
    omit_from_record: set[str] | None = None,
    unhashed: set[str] | None = None,
    record_overrides: dict[str, tuple[str, str]] | None = None,
    record_extra_rows: list[tuple[str, str, str]] | None = None,
    extra_unrecorded: list[tuple[str, bytes]] | None = None,
    symlink: tuple[str, bytes] | None = None,
) -> Path:
    """Create a tiny Wheel fixture without invoking a build backend."""

    entries = list(
        members
        or [
            ("demo/__init__.py", b"__version__ = '1.0'\n"),
            (
                f"{DIST_INFO}/METADATA",
                b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
            ),
            (
                f"{DIST_INFO}/WHEEL",
                b"Wheel-Version: 1.0\nTag: py3-none-any\n",
            ),
        ]
    )
    if symlink is not None:
        entries.append(symlink)

    omitted = omit_from_record or set()
    blank = unhashed or set()
    overrides = record_overrides or {}
    rows: list[tuple[str, str, str]] = []
    for name, data in entries:
        if name in omitted:
            continue
        if name in overrides:
            encoded_hash, encoded_size = overrides[name]
        elif name in blank:
            encoded_hash, encoded_size = "", ""
        else:
            encoded_hash, encoded_size = _hash_field(data, algorithm), str(len(data))
        rows.append((name, encoded_hash, encoded_size))
    rows.extend(record_extra_rows or [])
    rows.append((RECORD_PATH, "", ""))
    record = "".join(",".join(row) + "\n" for row in rows).encode("utf-8")

    wheel = directory / filename
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if symlink is not None and name == symlink[0]:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
        archive.writestr(RECORD_PATH, record)
        for name, data in extra_unrecorded or []:
            archive.writestr(name, data)
    return wheel


def component(
    component_id: str,
    version: str = "1.0",
    *,
    distribution: str = "embedded",
    licenses: list[str] | None = None,
    owned_paths: list[str] | None = None,
) -> dict[str, object]:
    category = {
        "bundled": "distributed",
        "external-runtime": "runtime-external",
    }.get(distribution, distribution)
    patterns = [
        path if "*" in path else f"{path.rstrip('/')}/**"
        for path in (owned_paths or [f"{component_id}/"])
    ]
    return {
        "id": component_id,
        "name": component_id,
        "type": "library",
        "third_party": True,
        "version": {"value": version, "status": "confirmed"},
        "origin": {"url": f"https://example.test/{component_id}", "status": "confirmed"},
        "license": {
            "concluded": (licenses or ["MIT"])[0],
            "status": "approved",
            "text_location": f"licenses/{component_id}.txt",
        },
        "copyrights": [f"{component_id} contributors"],
        "usages": [
            {
                "category": category,
                "target": "core-wheel",
                "path_patterns": patterns,
                "artifact_patterns": patterns,
            }
        ],
    }


def scanned_coverage(component_id: str, component_version: str) -> dict[str, str]:
    return {
        "component_id": component_id,
        "component_version": component_version,
        "status": "scanned",
        "scanner": "osv-scanner",
        "scanner_version": "2.5.1",
        "scanned_on": "2026-08-20",
        "evidence": "osv-results.json#sha256:test-fixture",
    }


def artifact(artifact_id: str, sha256: str, tag: str) -> dict[str, str]:
    return {
        "id": artifact_id,
        "filename": f"triton_anchor-0.2.0-{tag}.whl",
        "sha256": sha256,
        "version": "0.2.0",
        "tag": tag,
    }
