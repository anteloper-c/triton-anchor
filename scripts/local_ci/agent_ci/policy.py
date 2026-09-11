"""Classify a frozen diff into a trusted floor and optional risk-based checks."""

from __future__ import annotations

import ast
import io
import subprocess
import tokenize
from pathlib import Path

from .protocol import ContractError, POLICY_VERSION

# The runner owns the tool catalogue and dependency graph. Policy only selects
# required behaviour; it must never introduce a parallel execution registry.
from tools.basic_tools.runner import TOOL_IDS, dependencies

TOOLS = tuple(TOOL_IDS)
FRONTEND = {
    "environment",
    "frontend_build",
    "frontend_install",
    "frontend_tests",
    "frontend_smoke",
}
BACKEND = {
    "backend_build",
    "backend_install",
    "backend_tests",
    "backend_smoke",
    "flaggems",
    "compile_time",
    "pass_profile",
    "ir_serialization",
}
CHECK_ORDER = TOOLS

MAX_AST_BYTES = 2 * 1024 * 1024


def _git_blob(repo: Path, revision: str, path: str) -> bytes:
    command = [
        "git",
        "-c",
        f"safe.directory={repo.resolve()}",
        "-c",
        "core.fsmonitor=false",
    ]
    spec = f"{revision}:{path}"
    size = int(
        subprocess.check_output(
            [*command, "cat-file", "-s", spec], cwd=repo, stderr=subprocess.DEVNULL
        )
    )
    if size > MAX_AST_BYTES:
        raise ValueError("Python source is too large for trusted AST classification")
    return subprocess.check_output(
        [*command, "cat-file", "blob", spec], cwd=repo, stderr=subprocess.DEVNULL
    )


def _python_ast(blob: bytes, path: str) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(blob).readline)
    tree = ast.parse(blob.decode(encoding), filename=path, type_comments=True)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _annotate_semantics(repo: Path, base: str, tested: str, change: dict) -> dict:
    """Mark only same-path, regular Python edits whose parsed program is identical."""
    path = change["path"]
    if (
        change["status"] != "M"
        or change["old_path"] != path
        or not path.lower().endswith(".py")
        or change["old_mode"] != "100644"
        or change["mode"] != "100644"
    ):
        return change
    try:
        before = _python_ast(_git_blob(repo, base, path), path)
        after = _python_ast(_git_blob(repo, tested, path), path)
    except (
        LookupError,
        OSError,
        subprocess.CalledProcessError,
        SyntaxError,
        UnicodeError,
        ValueError,
    ):
        return change
    if before == after:
        return {**change, "semantic": "python_ast_equivalent"}
    return change


def changed_files(repo: Path, base: str, tested: str) -> list[dict]:
    raw = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={repo.resolve()}",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--raw",
            "-z",
            "--no-ext-diff",
            "--no-abbrev",
            "--find-renames",
            base,
            tested,
            "--",
        ],
        cwd=repo,
    )
    parts, index, result = raw.decode("utf-8", "surrogateescape").split("\0"), 0, []
    while index < len(parts) and parts[index]:
        header = parts[index].split()
        if len(header) != 5 or not header[0].startswith(":") or index + 1 >= len(parts):
            raise ContractError("Malformed trusted git diff")
        old_mode, new_mode, _, _, status = header
        old_path = parts[index + 1]
        new_path = old_path
        index += 2
        if status.startswith(("R", "C")):
            if index >= len(parts):
                raise ContractError("Malformed rename")
            new_path = parts[index]
            index += 1
        change = {
            "old_path": old_path,
            "path": new_path,
            "status": status,
            "old_mode": old_mode[1:],
            "mode": new_mode,
        }
        result.append(_annotate_semantics(repo, base, tested, change))
    return result


def category(path: str) -> str:
    p = path.lower()
    if p.endswith("llvm-hash.txt"):
        return "llvm"
    if p.endswith("cmakelists.txt") or p.endswith(".cmake"):
        return "compiler"
    if (
        p == ".gitmodules"
        or p.startswith("docker/")
        or "dockerfile" in p
        or p.endswith("envsetup.sh")
    ):
        return "environment"
    # Control-plane tests are control regressions, rather than product tests.
    if p.startswith(
        (
            ".github/",
            "scripts/ci/",
            "scripts/local_ci/",
            "dashboard/",
            "scripts/api_contract/",
        )
    ):
        if p.startswith("scripts/local_ci/ops_maint/") and (
            "/profiles/" in p
            or Path(p).name
            in {"runtime.py", "image_prepare.py", "dockerfile", "config.example.json"}
        ):
            return "environment"
        return "control"
    parts = p.split("/")
    if (
        any(part in {"test", "tests"} for part in parts)
        or Path(p).name.startswith("test_")
    ) and (
        p.endswith(
            (
                ".py",
                ".c",
                ".cc",
                ".cpp",
                ".h",
                ".hpp",
                ".sh",
                ".json",
                ".toml",
                ".yaml",
                ".yml",
            )
        )
        or Path(p).name in {"pytest.ini", "tox.ini"}
    ):
        return "test"
    if p.endswith(("agents.md", "skill.md", "ai_ci_program.md")):
        return "control"
    if p.startswith("api_contract/"):
        return "interface"
    if p.startswith("scripts/api_contract/"):
        return "control"
    if p in {"setup.py", "pyproject.toml", "manifest.in", "setup.cfg"}:
        return "packaging"
    if "requirements" in p or p.endswith((".lock", ".env")):
        return "environment"
    if p.startswith(
        (
            "csrc/",
            "triton/",
            "python/triton_anchor/adapters/",
            "python/triton_anchor/extensions/",
        )
    ) or any(
        name in p for name in ("pipeline", "anchor_ir", "hw_capability", "lowering")
    ):
        return "compiler"
    if p.startswith("python/triton_anchor/"):
        if any(v in p for v in ("jit", "cache", "concurrent")):
            return "compiler"
        return "frontend"
    if (
        p.startswith("docs/")
        or p in {"readme.md", "roadmap.md", "security.md", "license"}
    ) and p.endswith((".md", ".rst", ".txt")):
        return "docs"
    return "unknown"


def closure(checks: set[str]) -> set[str]:
    result = set(checks)
    for check in list(checks):
        result |= closure(set(dependencies(check)))
    return result


def ordered(checks: set[str]) -> list[str]:
    return [tool for tool in CHECK_ORDER if tool in checks]


def minimum_checks(
    changes: list[dict], *, backend_enabled: bool, full: bool = False
) -> dict:
    if not changes:
        raise ContractError(
            "Empty diff needs an explicit branch validation task; it is not documentation"
        )
    groups: set[str] = set()
    equivalent_python: list[str] = []
    test_paths: list[str] = []
    deleted_test = False
    for item in changes:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("old_path", item["path"]), str)
        ):
            raise ContractError("Invalid change manifest")
        item_groups = {
            category(item["path"]),
            category(item.get("old_path", item["path"])),
        }
        if item.get("mode") in {"160000", "120000"} or item.get("old_mode") in {
            "160000",
            "120000",
        }:
            item_groups.add("environment")
        groups |= item_groups
        if item.get("semantic") == "python_ast_equivalent":
            equivalent_python.append(item["path"])
        if "test" in item_groups:
            if item.get("status", "").startswith("D"):
                deleted_test = True
            else:
                test_paths.append(item["path"])

    # AST equality ignores line numbers and source text used by JIT/cache logic.
    # Keep the annotation for selection/review, never use it to waive execution.
    runtime_groups = groups - {"docs"}
    checks = {"control_plane"} if not runtime_groups else {"environment"}
    recommended: set[str] = set()
    required_parameters: dict[str, dict] = {}
    if "control" in runtime_groups:
        checks.add("control_plane")
    if runtime_groups & {"frontend", "interface", "packaging"}:
        checks |= FRONTEND
        recommended |= {"backend_smoke", "flaggems"}
    if "compiler" in runtime_groups:
        checks |= FRONTEND | {"backend_tests", "backend_smoke", "flaggems"}
        recommended |= {"compile_time", "pass_profile", "ir_serialization"}
    if "test" in runtime_groups:
        if "tests/test_smoke.py" in test_paths:
            checks.add("frontend_smoke")
            test_paths.remove("tests/test_smoke.py")
        if test_paths or not checks.intersection({"frontend_smoke"}):
            checks.add("frontend_tests")
        # Deleted tests and test-support changes require the corresponding suite.
        # Individual runnable Python tests can be selected exactly.
        selectable = [
            path
            for path in test_paths
            if Path(path).name.startswith("test_") and path.endswith(".py")
        ]
        if selectable and len(selectable) == len(test_paths) and not deleted_test:
            required_parameters["frontend_tests"] = {"paths": sorted(set(selectable))}
    full_scope = full or bool(
        runtime_groups & {"environment", "llvm", "performance", "unknown"}
    )
    if full_scope:
        checks |= set(TOOLS)
        if backend_enabled:
            required_parameters["flaggems"] = {"mode": "full"}
        required_parameters.pop("frontend_tests", None)

    if full_scope:
        level = "full"
    elif "compiler" in runtime_groups:
        level = "core"
    elif runtime_groups & {"frontend", "interface", "packaging"}:
        level = "frontend"
    elif "control" in runtime_groups:
        level = "control"
    elif runtime_groups:
        level = "test_only"
    else:
        level = "non_executable"

    all_required = closure(checks)
    unavailable = BACKEND if not backend_enabled else set()
    recommended_with_dependencies = closure(recommended)
    available_recommended = recommended_with_dependencies - all_required - unavailable
    return {
        "version": POLICY_VERSION,
        "categories": sorted(groups),
        "impact": {
            "level": level,
            "classification": "risk_assessed"
            if runtime_groups
            else "documentation_only",
            "active_categories": sorted(runtime_groups),
            "python_ast_equivalent": sorted(equivalent_python),
        },
        "required_checks": ordered(all_required - unavailable),
        "required_parameters": required_parameters,
        "recommended_checks": ordered(available_recommended),
        "not_applicable": ordered(
            (all_required | recommended_with_dependencies) & unavailable
        ),
        "capabilities": [t for t in CHECK_ORDER if t not in unavailable],
        "required_reviews": ["pr_info", "architecture"],
        "reason": (
            "The frozen diff defines a coverage floor. Changed tests execute with their dependencies; "
            "AST equality does not waive code tests. Full includes all supported FlagGems operators."
        ),
    }
