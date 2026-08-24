"""Produce a semantic delta for dependency declarations without executing code."""

from __future__ import annotations

import argparse
import ast
import configparser
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


def _literal_string_list(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    values: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(
            element.value, str
        ):
            return None
        values.append(element.value)
    return values


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
    declarations: list[dict[str, Any]] = []
    issues: list[str] = []

    def add_requirements(
        raw: Any, usage: str, source_kind: str, field: str
    ) -> None:
        if raw is None:
            return
        if not isinstance(raw, list) or any(
            not isinstance(requirement, str) for requirement in raw
        ):
            issues.append(f"{field} must be an array of requirement strings")
            return
        for requirement in raw:
            name = _requirement_name(requirement)
            if name:
                declarations.append(
                    _declaration(
                        name,
                        "pyproject.toml",
                        usage,
                        source_kind,
                        re.sub(r"\s+", "", requirement),
                    )
                )

    build_system = document.get("build-system")
    if isinstance(build_system, Mapping):
        add_requirements(
            build_system.get("requires"),
            "build-only",
            "pyproject-build-system",
            "pyproject.toml build-system.requires",
        )
    project = document.get("project")
    if isinstance(project, Mapping):
        dynamic = project.get("dynamic", [])
        if isinstance(dynamic, list) and any(
            field in dynamic for field in ("dependencies", "optional-dependencies")
        ):
            issues.append("pyproject.toml runtime dependencies are dynamic")
        add_requirements(
            project.get("dependencies"),
            "runtime-external",
            "pyproject-project-dependency",
            "pyproject.toml project.dependencies",
        )
        optional = project.get("optional-dependencies")
        if optional is not None:
            if not isinstance(optional, Mapping):
                issues.append(
                    "pyproject.toml project.optional-dependencies must be a table"
                )
            else:
                for group, requirements in optional.items():
                    add_requirements(
                        requirements,
                        "runtime-external",
                        "pyproject-optional-dependency",
                        f"pyproject.toml project.optional-dependencies.{group}",
                    )
    return declarations, issues


def _setup_declarations(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = root / "setup.py"
    if not path.exists():
        return [], []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename="setup.py")
    except (OSError, SyntaxError, UnicodeError) as exc:
        return [], [f"cannot parse setup.py declarations: {exc}"]

    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function_name = None
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            if function_name == "setup":
                for keyword in node.keywords:
                    if (
                        keyword.arg == "python_requires"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        declarations.append(
                            _declaration(
                                "Python3",
                                "setup.py",
                                "runtime-external",
                                "setup-python-requires",
                                keyword.value.value,
                            )
                        )
                    if keyword.arg == "install_requires":
                        requirements = _literal_string_list(keyword.value)
                        if requirements is None:
                            issues.append(
                                "setup.py install_requires must be a literal string array"
                            )
                        else:
                            for requirement in requirements:
                                name = _requirement_name(requirement)
                                if name:
                                    declarations.append(
                                        _declaration(
                                            name,
                                            "setup.py",
                                            "runtime-external",
                                            "setup-install-requirement",
                                            re.sub(r"\s+", "", requirement),
                                        )
                                    )
                    if keyword.arg == "extras_require":
                        if not isinstance(keyword.value, ast.Dict):
                            issues.append(
                                "setup.py extras_require must be a literal requirement table"
                            )
                            continue
                        for group, value in zip(
                            keyword.value.keys, keyword.value.values
                        ):
                            requirements = _literal_string_list(value)
                            if (
                                not isinstance(group, ast.Constant)
                                or not isinstance(group.value, str)
                                or requirements is None
                            ):
                                issues.append(
                                    "setup.py extras_require must be a literal requirement table"
                                )
                                continue
                            for requirement in requirements:
                                name = _requirement_name(requirement)
                                if name:
                                    declarations.append(
                                        _declaration(
                                            name,
                                            "setup.py",
                                            "runtime-external",
                                            "setup-extra-requirement",
                                            re.sub(r"\s+", "", requirement),
                                        )
                                    )
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            if "-G" in values and "Ninja" in values:
                declarations.append(
                    _declaration(
                        "Ninja", "setup.py", "build-only", "setup-cmake-generator"
                    )
                )
    return declarations, issues


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
            if command == "cmake_minimum_required" and "VERSION" in tokens:
                version_index = tokens.index("VERSION") + 1
                if version_index < len(tokens):
                    declarations.append(
                        _declaration(
                            "CMake",
                            relative,
                            "build-only",
                            "cmake-minimum-required",
                            f">={tokens[version_index]}",
                        )
                    )
            elif command == "find_package" and tokens:
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
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [], [f"cannot read .gitmodules: {exc}"]
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        return [], [f"cannot parse .gitmodules: {exc}"]

    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    for section in parser.sections():
        match = re.fullmatch(r'submodule\s+"([^"]+)"', section)
        if not match:
            continue
        name = match.group(1)
        submodule_path = parser.get(section, "path", fallback="").strip()
        origin_url = parser.get(section, "url", fallback="").strip()
        if not submodule_path or not origin_url:
            issues.append(f"submodule {name!r} must declare path and url")
            continue
        declaration = _declaration(
            name,
            ".gitmodules",
            "test-only",
            "git-submodule",
            origin_url,
        )
        declaration["submodule_path"] = PurePosixPath(submodule_path).as_posix()
        declaration["origin_url"] = origin_url
        declarations.append(declaration)
    return declarations, issues


def scan_declarations(root: str | Path) -> dict[str, Any]:
    repository = Path(root).resolve()
    declarations: list[dict[str, Any]] = []
    issues: list[str] = []
    for scanner in (
        _python_declarations,
        _pyproject_declarations,
        _setup_declarations,
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
    def identity(
        item: Mapping[str, Any],
    ) -> tuple[str, tuple[str, ...], str, str, str]:
        return (
            str(item.get("name", "")).casefold(),
            tuple(sorted(str(value) for value in item.get("candidate_usages", []))),
            str(item.get("declaration_value", "")),
            str(item.get("submodule_path", "")),
            str(item.get("origin_url", "")),
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
    parser.add_argument(
        "--root", help="write the full dependency inventory for one source tree"
    )
    parser.add_argument("--baseline-root")
    parser.add_argument("--current-root")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root:
        if args.baseline_root or args.current_root:
            raise SystemExit("--root cannot be combined with delta inputs")
        inventory = scan_declarations(args.root)
        write_json(args.output, inventory)
        return 0 if inventory["status"] == "success" else 1
    if not args.baseline_root or not args.current_root:
        raise SystemExit(
            "either --root or both --baseline-root and --current-root are required"
        )
    baseline = scan_declarations(args.baseline_root)
    current = scan_declarations(args.current_root)
    delta = declaration_delta(baseline, current)
    write_json(args.output, delta)
    return 0 if delta["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
