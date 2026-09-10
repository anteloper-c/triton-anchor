"""Classify a frozen diff into a trusted floor and optional risk-based checks."""
from __future__ import annotations

import ast
import io
import subprocess
import tokenize
from pathlib import Path

from .protocol import ContractError, POLICY_VERSION

TOOLS = (
    "environment", "frontend_build", "wheel_install_import", "frontend_smoke",
    "backend_rebuild", "backend_smoke_jit", "flaggems", "compile_time",
    "pass_profile", "ir_serialization",
)
FRONTEND = set(TOOLS[:4])
BACKEND = set(TOOLS[4:])
DEPENDENCIES = {
    "environment": [], "frontend_build": ["environment"],
    "wheel_install_import": ["frontend_build"], "frontend_smoke": ["wheel_install_import"],
    "backend_rebuild": ["frontend_smoke"], "backend_smoke_jit": ["backend_rebuild"],
    "flaggems": ["backend_smoke_jit"], "compile_time": ["backend_smoke_jit"],
    "pass_profile": ["backend_smoke_jit"], "ir_serialization": ["backend_smoke_jit"],
    "contract_tests": ["environment"],
}

CHECK_ORDER = (*TOOLS, "contract_tests")
MAX_AST_BYTES = 2 * 1024 * 1024


def _git_blob(repo: Path, revision: str, path: str) -> bytes:
    command = [
        "git", "-c", f"safe.directory={repo.resolve()}", "-c", "core.fsmonitor=false",
    ]
    spec = f"{revision}:{path}"
    size = int(subprocess.check_output([*command, "cat-file", "-s", spec], cwd=repo,
                                       stderr=subprocess.DEVNULL))
    if size > MAX_AST_BYTES:
        raise ValueError("Python source is too large for trusted AST classification")
    return subprocess.check_output([*command, "cat-file", "blob", spec], cwd=repo,
                                   stderr=subprocess.DEVNULL)


def _python_ast(blob: bytes, path: str) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(blob).readline)
    tree = ast.parse(blob.decode(encoding), filename=path, type_comments=True)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _annotate_semantics(repo: Path, base: str, tested: str, change: dict) -> dict:
    """Mark only same-path, regular Python edits whose parsed program is identical."""
    path = change["path"]
    if (change["status"] != "M" or change["old_path"] != path or not path.lower().endswith(".py")
            or change["old_mode"] != "100644" or change["mode"] != "100644"):
        return change
    try:
        before = _python_ast(_git_blob(repo, base, path), path)
        after = _python_ast(_git_blob(repo, tested, path), path)
    except (LookupError, OSError, subprocess.CalledProcessError, SyntaxError, UnicodeError, ValueError):
        return change
    if before == after:
        return {**change, "semantic": "python_ast_equivalent"}
    return change


def changed_files(repo: Path, base: str, tested: str) -> list[dict]:
    raw = subprocess.check_output([
        "git", "-c", f"safe.directory={repo.resolve()}", "-c", "core.fsmonitor=false", "diff", "--raw", "-z", "--no-ext-diff",
        "--no-abbrev", "--find-renames", base, tested, "--",
    ], cwd=repo)
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
        change = {"old_path": old_path, "path": new_path, "status": status,
                  "old_mode": old_mode[1:], "mode": new_mode}
        result.append(_annotate_semantics(repo, base, tested, change))
    return result


def category(path: str) -> str:
    p = path.lower()
    if p.endswith("llvm-hash.txt"):
        return "llvm"
    if p.endswith("cmakelists.txt") or p.endswith(".cmake"):
        return "compiler"
    if p == ".gitmodules" or p.startswith("docker/") or "dockerfile" in p or p.endswith("envsetup.sh"):
        return "environment"
    parts = p.split("/")
    if ((any(part in {"test", "tests"} for part in parts)
         or Path(p).name.startswith("test_"))
            and (p.endswith((".py", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".json", ".toml", ".yaml", ".yml"))
                 or Path(p).name in {"pytest.ini", "tox.ini"})):
        return "test"
    if p.startswith(("scripts/local_ci/tools/", "scripts/local_ci/environments/")) or p in {"scripts/local_ci/agent_ci/executor.py", "scripts/local_ci/deploy/config.example.json"}:
        return "environment"
    if p.startswith("scripts/local_ci/deterministic_ci/performance/"):
        return "performance"
    if p.startswith("scripts/local_ci/deterministic_ci/flaggems/"):
        return "environment"
    if p.startswith((".github/", "scripts/ci/", "scripts/local_ci/", "dashboard/")) or p.endswith(("agents.md", "skill.md", "ai_ci_program.md")):
        return "control"
    if p.startswith("api_contract/"):
        return "interface"
    if p.startswith("scripts/api_contract/"):
        return "control"
    if p in {"setup.py", "pyproject.toml", "manifest.in", "setup.cfg"}:
        return "packaging"
    if "requirements" in p or p.endswith((".lock", ".env")):
        return "environment"
    if (p.startswith(("csrc/", "triton/", "python/triton_anchor/adapters/", "python/triton_anchor/extensions/"))
            or any(name in p for name in ("pipeline", "anchor_ir", "hw_capability", "lowering"))):
        return "compiler"
    if p.startswith("python/triton_anchor/"):
        if any(v in p for v in ("jit", "cache", "concurrent")):
            return "compiler"
        return "frontend"
    if (p.startswith("docs/") or p in {"readme.md", "roadmap.md", "security.md", "license"}) and p.endswith((".md", ".rst", ".txt")):
        return "docs"
    return "unknown"


def closure(checks: set[str]) -> set[str]:
    result = set(checks)
    for check in list(checks):
        result |= closure(set(DEPENDENCIES[check]))
    return result


def ordered(checks: set[str]) -> list[str]:
    return [tool for tool in CHECK_ORDER if tool in checks]


def minimum_checks(changes: list[dict], *, backend_enabled: bool, full: bool = False) -> dict:
    if not changes:
        raise ContractError("Empty diff needs an explicit branch validation task; it is not documentation")
    groups: set[str] = set()
    active_groups: set[str] = set()
    equivalent_python: list[str] = []
    for item in changes:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]
                or not isinstance(item.get("old_path", item["path"]), str)):
            raise ContractError("Invalid change manifest")
        item_groups = {category(item["path"]), category(item.get("old_path", item["path"]))}
        mode_risk = (item.get("mode") in {"160000", "120000", "100755"}
                     or item.get("old_mode") in {"160000", "120000", "100755"})
        if mode_risk:
            item_groups.add("environment")
        groups |= item_groups
        trusted_equivalent = (item.get("semantic") == "python_ast_equivalent"
                              and item.get("status") == "M"
                              and item.get("old_path", item["path"]) == item["path"]
                              and item["path"].lower().endswith(".py")
                              and item.get("old_mode") == item.get("mode") == "100644")
        if trusted_equivalent and not mode_risk:
            equivalent_python.append(item["path"])
        else:
            active_groups |= item_groups

    runtime_groups = active_groups - {"docs"}
    checks = {"environment"}
    recommended: set[str] = set()
    if "control" in runtime_groups:
        checks.add("contract_tests")
    if runtime_groups & {"frontend", "interface", "packaging"}:
        checks |= FRONTEND
        recommended |= {"backend_rebuild", "backend_smoke_jit", "flaggems"}
    if "compiler" in runtime_groups:
        checks |= FRONTEND | {"backend_rebuild", "backend_smoke_jit"}
        recommended |= {"flaggems", "compile_time", "pass_profile", "ir_serialization"}
    if "test" in runtime_groups:
        recommended |= FRONTEND
    if runtime_groups & {"environment", "llvm", "performance", "unknown"} or full:
        checks |= set(TOOLS)

    if full or runtime_groups & {"environment", "llvm", "performance", "unknown"}:
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
    classification = ("trusted_python_ast_equivalent" if equivalent_python and not runtime_groups
                      else "documentation_only" if not runtime_groups
                      else "risk_assessed")
    return {
        "version": POLICY_VERSION, "categories": sorted(groups),
        "impact": {"level": level, "classification": classification,
                   "active_categories": sorted(runtime_groups),
                   "python_ast_equivalent": sorted(equivalent_python)},
        "required_checks": ordered(all_required - unavailable),
        "recommended_checks": ordered(available_recommended),
        "not_applicable": ordered((all_required | recommended_with_dependencies) & unavailable),
        "capabilities": [t for t in CHECK_ORDER if t not in unavailable],
        "required_reviews": ["pr_info", "architecture"],
        "reason": ("The frozen diff defines a semantic impact floor. Required checks cannot be skipped; "
                   "optional checks need a concrete changed-path, failure-risk and coverage rationale."),
    }
