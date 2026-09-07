"""The model may expand this trusted minimum, never subtract from it."""
from __future__ import annotations

import subprocess
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
        result.append({"old_path": old_path, "path": new_path, "status": status,
                       "old_mode": old_mode[1:], "mode": new_mode})
    return result


def category(path: str) -> str:
    p = path.lower()
    if p.endswith("llvm-hash.txt"):
        return "llvm"
    if p.endswith("cmakelists.txt") or p.endswith(".cmake"):
        return "compiler"
    if p == ".gitmodules" or p.startswith("docker/") or "dockerfile" in p or p.endswith("envsetup.sh"):
        return "environment"
    if p.startswith(("scripts/local_ci/tools/", "scripts/local_ci/environments/")) or p in {"scripts/local_ci/agent_ci/executor.py", "scripts/local_ci/deploy/config.example.json", "scripts/local_ci/config.example.env"}:
        return "environment"
    if p.startswith("scripts/local_ci/deterministic_ci/performance/"):
        return "performance"
    if p.startswith("scripts/local_ci/deterministic_ci/flaggems/"):
        return "environment"
    if p.startswith("scripts/ci/") and any(v in p for v in ("install", "build_frontend", "prebuilt", "configure_backend")):
        return "environment"
    if p.startswith((".github/", "scripts/ci/", "scripts/local_ci/", "scripts/dashboard/", "dashboard/")) or p.endswith(("agents.md", "skill.md", "ai_ci_program.md")):
        return "control"
    if p.startswith("api_contract/"):
        return "interface"
    if p.startswith("scripts/api_contract/"):
        return "control"
    if p in {"setup.py", "pyproject.toml", "manifest.in", "setup.cfg"}:
        return "packaging"
    if "requirements" in p or p.endswith((".lock", ".env")):
        return "environment"
    if p.startswith(("csrc/", "triton/")) or "pipeline" in p or "anchor_ir" in p:
        return "compiler"
    if p.startswith("python/triton_anchor/"):
        if any(v in p for v in ("jit", "cache", "concurrent")):
            return "compiler"
        return "frontend"
    if p.startswith("tests/"):
        return "unknown"
    if (p.startswith("docs/") or p in {"readme.md", "roadmap.md", "security.md", "license"}) and p.endswith((".md", ".rst", ".txt")):
        return "docs"
    return "unknown"


def closure(checks: set[str]) -> set[str]:
    result = set(checks)
    for check in list(checks):
        result |= closure(set(DEPENDENCIES[check]))
    return result


def minimum_checks(changes: list[dict], *, backend_enabled: bool, full: bool = False) -> dict:
    if not changes:
        raise ContractError("Empty diff needs an explicit branch validation task; it is not documentation")
    groups: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or not item.get("path"):
            raise ContractError("Invalid change manifest")
        groups |= {category(item["path"]), category(item.get("old_path", item["path"]))}
        if item.get("mode") in {"160000", "120000", "100755"} or item.get("old_mode") in {"160000", "120000", "100755"}:
            if groups <= {"docs"} or item.get("mode") in {"160000", "120000"}:
                groups.add("environment")
    checks = {"environment"}
    if groups & {"frontend", "interface"}:
        checks |= FRONTEND | {"backend_rebuild", "backend_smoke_jit", "flaggems"}
    if "packaging" in groups:
        checks |= FRONTEND | {"backend_rebuild", "backend_smoke_jit"}
    if groups & {"compiler", "environment", "llvm", "performance", "unknown"} or full:
        checks |= set(TOOLS)
    if "control" in groups or groups <= {"docs"}:
        checks.add("contract_tests")
    product_groups = groups & {"frontend", "interface", "packaging", "compiler", "environment"}
    if len(product_groups) > 1:
        checks |= set(TOOLS)
    all_required = closure(checks)
    unavailable = BACKEND if not backend_enabled else set()
    return {
        "version": POLICY_VERSION, "categories": sorted(groups),
        "required_checks": [t for t in (*TOOLS, "contract_tests") if t in all_required - unavailable],
        "not_applicable": sorted(all_required & unavailable),
        "capabilities": [t for t in (*TOOLS, "contract_tests") if t not in unavailable],
        "required_reviews": ["pr_info", "architecture"],
        "reason": "Trusted diff categories determine a union of mandatory checks; Codex may add checks.",
    }
