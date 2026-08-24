"""Collect build evidence for one triton-anchor Wheel."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .model import BUILD_EVIDENCE_BINDINGS


NATIVE_WHEEL_MEMBER = "triton/_C/libtriton.so"
_VERSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*))"
)
_NEEDED_PATTERN = re.compile(r"\(NEEDED\).*Shared library: \[([^\]]+)\]")


class BuildEvidenceError(RuntimeError):
    """Raised when the Wheel build evidence cannot be collected."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    match = _VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None


def _llvm_version() -> str | None:
    for variable in ("LLVM_SYSPATH", "LLVM_BUILD_DIR"):
        root = os.getenv(variable)
        if root:
            llvm_config = Path(root) / "bin" / "llvm-config"
            if llvm_config.is_file():
                return _command_version([str(llvm_config), "--version"])
    llvm_config = shutil.which("llvm-config")
    return _command_version([llvm_config, "--version"]) if llvm_config else None


def _source_commit(path: Path, pattern: re.Pattern[str]) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = pattern.search(text)
    return match.group(1) if match else None


def _inspect_native_dependencies(wheel: Path) -> list[str]:
    try:
        archive = zipfile.ZipFile(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildEvidenceError(f"cannot read Wheel {wheel}: {exc}") from exc

    with archive:
        members = [
            entry
            for entry in archive.infolist()
            if entry.filename == NATIVE_WHEEL_MEMBER and not entry.is_dir()
        ]
        if len(members) != 1:
            raise BuildEvidenceError(
                f"Wheel must contain exactly one {NATIVE_WHEEL_MEMBER}"
            )
        readelf = shutil.which("readelf")
        if readelf is None:
            raise BuildEvidenceError("readelf is required to inspect libtriton.so")

        with tempfile.TemporaryDirectory(prefix="triton-anchor-wheel-") as temporary:
            native = Path(temporary) / "libtriton.so"
            with archive.open(members[0]) as source, native.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            try:
                result = subprocess.run(
                    [readelf, "-d", str(native)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "LC_ALL": "C"},
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise BuildEvidenceError(
                    f"readelf failed for {NATIVE_WHEEL_MEMBER}: {exc}"
                ) from exc
    return sorted(set(_NEEDED_PATTERN.findall(result.stdout)))


def _is_glibc_soname(soname: str) -> bool:
    return bool(
        re.match(r"^lib(?:c|m|dl|rt|pthread|resolv|util)\.so(?:\.|$)", soname)
        or re.match(r"^ld-linux(?:-[A-Za-z0-9_]+)*\.so(?:\.|$)", soname)
    )


def _is_gcc_runtime_soname(soname: str) -> bool:
    return bool(
        re.match(r"^lib(?:gcc_s|stdc\+\+|gomp|atomic|quadmath)\.so(?:\.|$)", soname)
    )


def _component(
    component_id: str,
    version: str | None,
    usages: list[str],
    evidence: dict[str, Any],
    presence: str = "present",
) -> dict[str, Any]:
    return {
        "id": component_id,
        "version": version,
        "usages": usages,
        "presence": presence,
        "evidence": evidence,
    }


def _configured_cxx_compiler(command: str) -> dict[str, Any]:
    executable = shutil.which(command)
    if executable is None:
        raise BuildEvidenceError(f"configured C++ compiler is unavailable: {command}")
    compiler_name = Path(executable).name
    if not re.fullmatch(r"g\+\+(?:-\d+)?", compiler_name):
        raise BuildEvidenceError(
            f"configured C++ compiler is not mapped to a component: {executable}"
        )
    version = _command_version([executable, "-dumpfullversion"])
    if version is None:
        raise BuildEvidenceError(
            f"cannot determine the configured C++ compiler version: {executable}"
        )
    return _component(
        "gcc-toolchain",
        version,
        ["build-only"],
        {"source": "configured-cxx-compiler", "path": str(Path(executable).resolve())},
    )


def collect_build_evidence(
    wheel: Path,
    source_root: Path,
    evidence_binding: str,
    cxx_compiler: str | None = None,
    package_tool: str | None = None,
) -> dict[str, Any]:
    """Return core-consumable evidence for a Wheel built from ``source_root``."""

    if not wheel.is_file() or wheel.suffix != ".whl":
        raise BuildEvidenceError(f"--wheel must name one existing .whl file: {wheel}")
    if not source_root.is_dir():
        raise BuildEvidenceError(
            f"--source-root must name the source directory: {source_root}"
        )

    needed = _inspect_native_dependencies(wheel)
    native_evidence = {
        "source": "readelf-dt-needed",
        "native_member": NATIVE_WHEEL_MEMBER,
    }
    llvm_sonames = [
        name for name in needed if name.startswith(("libLLVM", "libMLIR"))
    ]
    zlib_sonames = [
        name for name in needed if re.match(r"^libz\.so(?:\.|$)", name)
    ]
    zstd_sonames = [
        name for name in needed if re.match(r"^libzstd\.so(?:\.|$)", name)
    ]
    glibc_sonames = [name for name in needed if _is_glibc_soname(name)]
    gcc_sonames = [name for name in needed if _is_gcc_runtime_soname(name)]
    llvm_source_commit = _source_commit(
        source_root / "triton" / "cmake" / "llvm-hash.txt",
        re.compile(r"^([0-9a-f]{40})$", re.MULTILINE),
    )
    llvm_tool_version = _llvm_version()
    mapped_sonames = set(
        llvm_sonames
        + zlib_sonames
        + zstd_sonames
        + glibc_sonames
        + gcc_sonames
    )
    unmapped_sonames = sorted(set(needed) - mapped_sonames)

    components = [
        _component(
            "cpython",
            platform.python_version(),
            ["build-only", "runtime-external"],
            {"source": "build-interpreter"},
        ),
        _component(
            "setuptools",
            _distribution_version("setuptools"),
            ["build-only"],
            {"source": "python-distribution", "distribution": "setuptools"},
        ),
        _component(
            "wheel-build-package",
            _distribution_version("wheel"),
            ["build-only"],
            {"source": "python-distribution", "distribution": "wheel"},
        ),
        _component(
            "pypa-build",
            _distribution_version("build"),
            ["build-only"],
            {"source": "python-distribution", "distribution": "build"},
        ),
        _component(
            "cmake",
            _command_version(["cmake", "--version"]),
            ["build-only"],
            {"source": "build-tool", "command": "cmake --version"},
        ),
        _component(
            "ninja",
            _command_version(["ninja", "--version"]),
            ["build-only"],
            {"source": "build-tool", "command": "ninja --version"},
        ),
        _component(
            "llvm-project",
            llvm_source_commit,
            ["build-only", "runtime-external" if llvm_sonames else "embedded"],
            {
                **native_evidence,
                "matching_sonames": llvm_sonames,
                "version_source": "triton/cmake/llvm-hash.txt",
                "tool_version": llvm_tool_version,
            },
        ),
        _component(
            "pybind11",
            _distribution_version("pybind11"),
            ["build-only", "embedded"],
            {"source": "python-distribution", "distribution": "pybind11"},
        ),
        _component(
            "triton",
            _source_commit(
                source_root / "triton" / "TRITON_VERSION",
                re.compile(r"^# Commit: ([0-9a-f]{40})$", re.MULTILINE),
            ),
            ["embedded"],
            {"source": "triton/TRITON_VERSION"},
        ),
        _component(
            "triton-linalg",
            None,
            ["embedded"],
            {"source": "vendored-source"},
        ),
        _component(
            "f2reduce",
            _source_commit(
                source_root
                / "triton"
                / "third_party"
                / "f2reduce"
                / "VERSION",
                re.compile(r"^([0-9a-f]{40})\.?$", re.MULTILINE),
            ),
            ["embedded"],
            {"source": "triton/third_party/f2reduce/VERSION"},
        ),
        _component(
            "zlib",
            None,
            ["build-only", "runtime-external" if zlib_sonames else "embedded"],
            {**native_evidence, "matching_sonames": zlib_sonames},
        ),
    ]

    if cxx_compiler:
        components.append(_configured_cxx_compiler(cxx_compiler))

    if package_tool:
        uv_selected = package_tool == "uv"
        components.append(
            _component(
                "uv",
                _command_version(["uv", "--version"]) if uv_selected else None,
                ["build-only"],
                {"source": "package-tool-selection", "selected": package_tool},
                "present" if uv_selected else "absent",
            )
        )
    if "TTGPU" in os.environ:
        components.append(
            _component(
                "ttgpu-variant-sources",
                None,
                ["embedded"],
                {
                    "source": "build-switch",
                    "name": "TTGPU",
                    "value": os.environ["TTGPU"],
                },
            )
        )
    components.append(
        _component(
            "zstd",
            None,
            ["runtime-external"],
            {**native_evidence, "sonames": zstd_sonames},
            "present" if zstd_sonames else "absent",
        )
    )
    for component_id, sonames in (
        ("glibc", glibc_sonames),
        ("gcc-runtime", gcc_sonames),
    ):
        if sonames:
            components.append(
                _component(
                    component_id,
                    None,
                    ["runtime-external"],
                    {**native_evidence, "sonames": sonames},
                )
            )

    return {
        "compliance_build": {
            "status": "success",
            "evidence_binding": evidence_binding,
            "artifact_sha256": _sha256_file(wheel),
            "native": {
                "member": NATIVE_WHEEL_MEMBER,
                "dt_needed": needed,
                "unmapped_sonames": unmapped_sonames,
            },
            "components": components,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument(
        "--evidence-binding",
        required=True,
        choices=sorted(BUILD_EVIDENCE_BINDINGS),
    )
    parser.add_argument("--cxx-compiler")
    parser.add_argument("--package-tool", choices=("pypa-build", "uv"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.evidence_binding == "same-build" and (
        not args.cxx_compiler or not args.package_tool
    ):
        parser.error(
            "--cxx-compiler and --package-tool are required for same-build evidence"
        )

    try:
        evidence = collect_build_evidence(
            Path(args.wheel),
            Path(args.source_root),
            args.evidence_binding,
            args.cxx_compiler,
            args.package_tool,
        )
    except BuildEvidenceError as exc:
        parser.error(str(exc))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
