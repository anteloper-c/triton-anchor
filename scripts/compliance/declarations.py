"""Produce a semantic delta for dependency declarations without executing code."""

from __future__ import annotations

import argparse
import ast
import re
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - CI uses Python 3.11+.
    tomllib = None  # type: ignore[assignment]

from .core import write_json


_CMAKE_CALL = re.compile(r"(?is)\b([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)")
_CMAKE_INTERNAL_COMMANDS = {
    "add_executable",
    "add_library",
    "add_custom_target",
    "add_llvm_library",
    "add_mlir_library",
    "add_mlir_dialect_library",
    "add_triton_library",
}
_CMAKE_LINK_COMMANDS = {"target_link_libraries"}
_CMAKE_LINK_MACROS = {"add_llvm_library", "add_mlir_library", "add_triton_library"}
_CMAKE_KEYWORDS = {
    "PRIVATE",
    "PUBLIC",
    "INTERFACE",
    "LINK_LIBS",
    "DEPENDS",
    "SOURCES",
    "SHARED",
    "STATIC",
    "OBJECT",
    "MODULE",
}
_CMAKE_SECTION_KEYWORDS = {
    "ADDITIONAL_HEADER_DIRS",
    "DEPENDS",
    "LINK_COMPONENTS",
    "LINK_LIBS",
    "SOURCES",
}
_PROJECT_IMPORT_ROOTS = {"triton", "triton_anchor"}
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")
_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".def", ".h", ".hpp", ".inc", ".td"}


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _declaration(
    name: str,
    path: str,
    usage: str,
    source_kind: str,
    declaration_value: str | None = None,
) -> dict[str, Any]:
    result = {
        "kind": "dependency-declaration",
        "name": name,
        "path": path,
        "candidate_usages": [usage],
        "declaration_source": source_kind,
    }
    if declaration_value:
        result["declaration_value"] = declaration_value
    return result


def _requirement_name(value: str) -> str | None:
    match = _REQUIREMENT_NAME.match(value.strip())
    return match.group(1) if match else None


def _python_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    standard_library = set(getattr(sys, "stdlib_module_names", ()))
    standard_library.update(sys.builtin_module_names)
    standard_library.add("__future__")
    candidate_roots = [root / "python", root / "triton" / "python"]
    paths = sorted(
        {
            path
            for candidate_root in candidate_roots
            if candidate_root.exists()
            for path in candidate_root.rglob("*.py")
        }
    )
    for path in paths:
        relative = _relative(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append(f"cannot parse Python imports from {relative}: {exc}")
            continue
        usage = "test-only" if "tests" in PurePosixPath(relative).parts else "runtime-external"
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".", 1)[0])
        for name in sorted(names - standard_library - _PROJECT_IMPORT_ROOTS):
            declarations.append(_declaration(name, relative, usage, "python-import"))
    return declarations, issues


def _pyproject_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = root / "pyproject.toml"
    if not path.exists():
        return [], []
    if tomllib is None:
        return [], ["Python 3.11 or newer is required to parse pyproject.toml"]
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return [], [f"cannot parse pyproject.toml: {exc}"]
    declarations = []
    for requirement in document.get("build-system", {}).get("requires", []):
        name = _requirement_name(str(requirement))
        if name:
            declarations.append(
                _declaration(
                    name,
                    "pyproject.toml",
                    "build-only",
                    "pyproject-build-system",
                    re.sub(r"\s+", "", str(requirement)),
                )
            )
    return declarations, []


def _normalize_cmake_name(name: str) -> str | None:
    token = name.strip().strip('"\'')
    if not token or token in _CMAKE_KEYWORDS:
        return None
    if token.startswith(("$", "-")) or "/" in token or "\\" in token:
        return None
    if Path(token).suffix.casefold() in _SOURCE_SUFFIXES:
        return None
    if token.startswith(("LLVM", "MLIR")):
        return "LLVM"
    if token.startswith("pybind11"):
        return "pybind11"
    if token.startswith("Python"):
        return "Python3"
    if token in {"z", "ZLIB", "ZLIB::ZLIB"}:
        return "zlib"
    if token.startswith("Triton") or token == "triton":
        return None
    return token


def _cmake_tokens(body: str) -> list[str]:
    try:
        return shlex.split(body, posix=True)
    except ValueError:
        return body.split()


def _link_macro_items(tokens: Sequence[str]) -> list[str]:
    try:
        start = tokens.index("LINK_LIBS") + 1
    except ValueError:
        return []
    items: list[str] = []
    for token in tokens[start:]:
        if token in _CMAKE_SECTION_KEYWORDS:
            break
        items.append(token)
    return items


def _cmake_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    paths = sorted({*root.rglob("CMakeLists.txt"), *root.rglob("*.cmake")})
    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    internal_targets: set[str] = set()
    calls_by_path: list[tuple[str, list[tuple[str, str]]]] = []
    for path in paths:
        relative = _relative(path, root)
        try:
            text = re.sub(r"(?m)#.*$", "", path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            issues.append(f"cannot read CMake declarations from {relative}: {exc}")
            continue
        calls = [(name.lower(), body) for name, body in _CMAKE_CALL.findall(text)]
        calls_by_path.append((relative, calls))
        for command, body in calls:
            tokens = _cmake_tokens(body)
            if command in _CMAKE_INTERNAL_COMMANDS and tokens:
                internal_targets.add(tokens[0].strip('"\''))

    for relative, calls in calls_by_path:
        for command, body in calls:
            tokens = _cmake_tokens(body)
            names: Iterable[str] = ()
            if command == "find_package" and tokens:
                names = tokens[:1]
            elif command == "fetchcontent_declare" and tokens:
                names = tokens[:1]
            elif command in _CMAKE_LINK_COMMANDS and len(tokens) > 1:
                names = tokens[1:]
            elif command in _CMAKE_LINK_MACROS:
                names = _link_macro_items(tokens)
            for raw_name in names:
                normalized = _normalize_cmake_name(raw_name)
                raw_token = raw_name.strip('"\'')
                if not normalized or raw_token in internal_targets:
                    continue
                declarations.append(
                    _declaration(normalized, relative, "build-only", f"cmake-{command}")
                )
    return declarations, issues


def _vendored_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    declarations = []
    for third_party in sorted(path for path in root.rglob("third_party") if path.is_dir()):
        for dependency in sorted(path for path in third_party.iterdir() if path.is_dir()):
            declarations.append(
                _declaration(
                    dependency.name,
                    _relative(dependency, root),
                    "embedded",
                    "vendored-directory",
                )
            )
    return declarations, []


def _gitmodule_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = root / ".gitmodules"
    if not path.exists():
        return [], []
    declarations = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [], [f"cannot read .gitmodules: {exc}"]
    for name in re.findall(r'(?m)^\s*\[submodule\s+"([^"]+)"\]\s*$', text):
        declarations.append(_declaration(name, ".gitmodules", "test-only", "git-submodule"))
    return declarations, []


def scan_declarations(root: str | Path) -> dict[str, Any]:
    repository = Path(root).resolve()
    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    for scanner in (
        _python_declarations,
        _pyproject_declarations,
        _cmake_declarations,
        _gitmodule_declarations,
        _vendored_declarations,
    ):
        found, scanner_issues = scanner(repository)
        declarations.extend(found)
        issues.extend(scanner_issues)
    unique = {
        (
            str(item["name"]).casefold(),
            tuple(item["candidate_usages"]),
            str(item["path"]),
            str(item["declaration_source"]),
            str(item.get("declaration_value", "")),
        ): item
        for item in declarations
    }
    items = [unique[key] for key in sorted(unique)]
    return {
        "source": "dependency-inventory",
        "status": "failed" if issues else "success",
        "items": items,
        "issues": sorted(set(issues)),
    }


def declaration_delta(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    def identity(item: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str]:
        return (
            str(item.get("name", "")).casefold(),
            tuple(sorted(str(value) for value in item.get("candidate_usages", []))),
            str(item.get("declaration_value", "")),
        )

    baseline_identities = {identity(item) for item in baseline.get("items", [])}
    added = [
        dict(item)
        for item in current.get("items", [])
        if identity(item) not in baseline_identities
    ]
    issues = [
        *[str(issue) for issue in baseline.get("issues", [])],
        *[str(issue) for issue in current.get("issues", [])],
    ]
    return {
        "source": "dependency-delta",
        "status": "failed"
        if baseline.get("status") != "success"
        or current.get("status") != "success"
        or issues
        else "success",
        "items": sorted(
            added,
            key=lambda item: (
                str(item.get("name", "")).casefold(),
                str(item.get("path", "")),
            ),
        ),
        "issues": sorted(set(issues)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", required=True)
    parser.add_argument("--current-root", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline = scan_declarations(args.baseline_root)
    current = scan_declarations(args.current_root)
    delta = declaration_delta(baseline, current)
    write_json(args.output, delta)
    return 0 if delta["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
