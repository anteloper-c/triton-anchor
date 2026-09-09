#!/usr/bin/env python3
"""Plan real commands for the evidence broker, or execute them locally for diagnosis.

Only the broker may turn command execution into trusted CI receipts. Context and
profile are supplied by the controller; the agent supplies only parameters.

frontend_* uses the tested source checkout; backend_* uses profile.tools.backend_dir.
Build creates a wheel, install verifies/installs that wheel, tests runs pytest,
and smoke checks import/JIT. Each stage is independently callable; shared stages
use the same implementation, with dependencies declared below.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

BACKEND_TOOLS = frozenset({"backend_build", "backend_install", "backend_tests", "backend_smoke",
                           "flaggems", "compile_time", "pass_profile", "ir_serialization"})
DEPENDENCIES = {
    "environment": [],
    "frontend_build": ["environment"],
    "frontend_install": ["frontend_build"],
    "frontend_tests": ["frontend_install"],
    "frontend_smoke": ["frontend_install"],
    "backend_build": ["environment"],
    "backend_install": ["backend_build", "frontend_install"],
    "backend_tests": ["frontend_install", "backend_install"],
    "backend_smoke": ["frontend_install", "backend_install"],
    "flaggems": ["backend_smoke"], "compile_time": ["backend_smoke"],
    "pass_profile": ["backend_smoke"], "ir_serialization": ["backend_smoke"],
}
TOOL_IDS = tuple(DEPENDENCIES)
MINIMUM_FRONTEND = ("environment", "frontend_build", "frontend_install", "frontend_smoke")


def bounded(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def path_join(root: str, *parts: str) -> str:
    # Plans run on the controller but normally describe Linux container paths.
    return str(PurePosixPath(root).joinpath(*parts)) if root.startswith("/") else str(Path(root).joinpath(*parts))


def test_selection(tool_id: str, config: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
    """Select pytest nodes only inside the trusted profile's test roots."""
    key = "frontend_test_paths" if tool_id == "frontend_tests" else "backend_test_paths"
    roots = config.get(key, ["python/triton_anchor/tests"] if tool_id == "frontend_tests" else None)

    def relative(value: Any, node: bool = False) -> PurePosixPath:
        if not isinstance(value, str) or not value or len(value) > 2048 or any(c in value for c in "\\\n\r\x00"):
            raise ValueError("Test selections must be relative paths or pytest node IDs")
        filename = value.split("::", 1)[0] if node else value
        path = PurePosixPath(filename)
        if not filename or path.is_absolute() or ".." in path.parts or ":" in filename or filename.startswith("-"):
            raise ValueError("Test paths must stay within their trusted roots")
        return path

    if not isinstance(roots, list) or not roots or len(roots) > 100:
        raise ValueError(f"profile.tools.{key} must configure nonempty real test paths")
    allowed = [relative(value) for value in roots]
    selected = parameters.get("paths", roots)
    if not isinstance(selected, list) or not selected or len(selected) > 100:
        raise ValueError("paths must select 1..100 test paths or pytest node IDs")
    for value in selected:
        path = relative(value, node=True)
        if not any(path == root or root in path.parents for root in allowed):
            raise ValueError("Test selection is outside the profile's trusted test roots")
    return list(dict.fromkeys(selected))


def plan(tool_id: str, context: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    if tool_id not in DEPENDENCIES:
        raise ValueError(f"Unknown basic tool: {tool_id}")
    params = dict(parameters or {})
    for key in ("source_dir", "artifact_dir", "task_id", "target_sha", "triton_version"):
        if not isinstance(context.get(key), str) or not context[key]:
            raise ValueError(f"context.{key} is required")
    if not re.fullmatch(r"[0-9a-f]{40}", context["target_sha"]):
        raise ValueError("target_sha must identify the exact tested commit")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", context["task_id"]):
        raise ValueError("Invalid task_id")
    result: dict[str, Any] = {"tool_id": tool_id, "status": "ready", "reason": "",
                              "commands": [], "artifacts": [], "dependencies": DEPENDENCIES[tool_id]}
    if tool_id in BACKEND_TOOLS and not re.fullmatch(r"3\.0(?:\.\d+)?", context["triton_version"]):
        result.update(status="not_applicable", reason="仅 Triton 3.0 环境部署了后端、算子及性能测试能力。")
        return result
    allowed = {"jobs", "build_mode"} if tool_id in {"frontend_build", "backend_build"} else set()
    if tool_id in {"frontend_tests", "backend_tests"}:
        allowed = {"paths", "keyword"}
    if tool_id == "flaggems":
        allowed = {"mode", "ops", "categories"}
    if tool_id in {"compile_time", "pass_profile", "ir_serialization"}:
        allowed = {"kernels", "repeat", "warmup"}
    if set(params) - allowed:
        raise ValueError(f"Unsupported parameters: {sorted(set(params) - allowed)}")
    completed = context.get("completed_tools")
    if completed is not None:
        missing = set(DEPENDENCIES[tool_id]) - set(completed)
        if missing:
            raise ValueError(f"Current-task successful tool receipts required: {sorted(missing)}")
    profile = context.get("profile", {})
    config = profile.get("tools", {})
    py = context.get("python_bin", config.get("python_bin", "python3"))
    trusted_py = context.get("trusted_python_bin", "/usr/bin/python3")
    root = context.get("tools_dir", str(Path(__file__).resolve().parents[1]))
    helper = path_join(root, "basic_tools", "actions.py")
    source = context["source_dir"]
    out = path_join(context["artifact_dir"], tool_id)
    env = {str(k): str(v) for k, v in config.get("env", {}).items()}
    env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", PYTHON_BIN=py,
               TMPDIR=path_join(out, "tmp"), TRITON_CACHE_DIR=path_join(out, "cache"),
               TRITON_DUMP_DIR=path_join(out, "dump"), UV_LINK_MODE="copy")
    if context.get("task_venv"):
        env["LOCAL_CI_TASK_VENV"] = context["task_venv"]
    if config.get("llvm_dir"):
        llvm = config["llvm_dir"]
        env.update(LLVM_BUILD_DIR=llvm, LLVM_SYSPATH=llvm, LLVM_INCLUDE_DIRS=path_join(llvm, "include"),
                   LLVM_LIBRARY_DIR=path_join(llvm, "lib"), LLVM_BINARY_DIR=path_join(llvm, "bin"))
    if "jobs" in allowed:
        jobs = bounded(params.get("jobs", 2), "jobs", 1, 64)
        if params.get("build_mode", "fresh") not in {"fresh", "incremental"}:
            raise ValueError("build_mode must be fresh or incremental")
        env.update(MAX_JOBS=str(jobs), CMAKE_BUILD_PARALLEL_LEVEL=str(jobs), NINJAFLAGS=f"-j{jobs}")

    def add(argv: list[str], cwd: str | None = None, timeout: int = 300) -> None:
        scripts = list(config.get("env_scripts", []))
        if tool_id in BACKEND_TOOLS:
            scripts += config.get("backend_env_scripts", [])
        for script in reversed(scripts):
            argv = ["bash", path_join(root, "basic_tools", "env_exec.sh"), script["path"],
                    *script.get("args", []), "--", *argv]
        result["commands"].append({"argv": argv, "cwd": cwd or source, "env": env.copy(), "timeout": timeout})

    def action(name: str, extra: dict[str, Any] | None = None, timeout: int = 300, cwd: str | None = None) -> None:
        # Keep unrelated PR prose, service credentials and container settings out
        # of command-line evidence. Helpers receive only the tool context.
        action_context = {key: context[key] for key in ("source_dir", "artifact_dir", "task_id", "target_sha",
                          "triton_version", "base_sha", "performance_baselines", "task_venv") if key in context}
        action_context["python_bin"] = py
        action_context["profile"] = {"id": profile.get("id"), "llvm_revision": profile.get("llvm_revision"), "tools": config}
        payload = {"tool_id": tool_id, "context": action_context, "parameters": params, **(extra or {})}
        add([trusted_py, "-I", "-S", helper, name,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":"))], cwd, timeout)

    action("preflight")
    if tool_id == "environment":
        action("environment")
    elif tool_id in {"frontend_build", "backend_build"}:
        build_source = source if tool_id == "frontend_build" else config.get("backend_dir")
        if not build_source:
            raise ValueError("profile.tools.backend_dir is required")
        action("prepare_build", {"build_source": build_source})
        add([py, "-m", "build", "--wheel", "--no-isolation", "--outdir", path_join(out, "wheels"), build_source], build_source, 7200)
        action("record_wheel")
    elif tool_id in {"frontend_install", "backend_install"}:
        action("install_wheel", timeout=1200)
        if tool_id == "frontend_install":
            # -I prevents candidate source/PYTHONPATH from shadowing the installed wheel.
            add([py, "-I", "-c", "import triton_anchor; print(triton_anchor.__file__); print(getattr(triton_anchor, '__version__', 'unknown'))"], out)
        else:
            action("backend_discovery", cwd=out)
    elif tool_id in {"frontend_tests", "backend_tests"}:
        test_source = source if tool_id == "frontend_tests" else config.get("backend_dir")
        if not test_source:
            raise ValueError("profile.tools.backend_dir is required")
        selected = test_selection(tool_id, config, params)
        keyword = params.get("keyword", "")
        if not isinstance(keyword, str) or len(keyword) > 300 or any(c in keyword for c in "\n\r\x00"):
            raise ValueError("keyword must be a pytest expression of at most 300 characters")
        action("prepare_tests", {"test_source": test_source, "test_paths": selected})
        # Python capture avoids anonymous-file truncation on Windows bind mounts.
        # Native stdout/stderr still reach the broker's outer command log.
        argv = [py, "-I", "-m", "pytest", "-q", "-o", "addopts=", "--capture=sys", "--import-mode=importlib",
                "--rootdir", test_source, "--junitxml", path_join(out, "tests.xml")]
        if keyword:
            argv += ["-k", keyword]
        argv += [path_join(test_source, value) for value in selected]
        env["PYTEST_ADDOPTS"] = ""
        add(argv, out, 3600)
        action("test_results", {"test_source": test_source, "test_paths": selected}, cwd=out)
    elif tool_id == "frontend_smoke":
        add([py, "-I", path_join(source, "tests", "test_smoke.py")], out, 900)
        action("smoke_success")
    elif tool_id == "backend_smoke":
        command = config.get("backend_smoke_argv")
        if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
            raise ValueError("profile.tools.backend_smoke_argv must configure a real backend JIT test")
        command = [py if part == "{python}" else part for part in command]
        if command[0] in {"python", "python3", config.get("python_bin", "python3")}:
            command[0] = py
        action("backend_discovery")
        add(command, config["backend_dir"], 1800)
        action("smoke_success")
    elif tool_id == "flaggems":
        mode = params.get("mode", "impact")
        if mode not in {"impact", "full"}:
            raise ValueError("FlagGems mode must be impact or full")
        if mode == "full" and context.get("manual_full") is not True:
            raise ValueError("Full FlagGems requires a trusted manual_full task")
        for name in ("ops", "categories"):
            values = params.get(name, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", v) for v in values):
                raise ValueError(f"{name} must be an array of operator/category identifiers")
        fg = config.get("flaggems_dir")
        if not fg:
            raise ValueError("profile.tools.flaggems_dir is required")
        argv = [py, path_join(root, "basic_tools", "flaggems", "batch_test_flaggems.py"),
                "--mode", mode, "--ops", ",".join(params.get("ops", [])), "--categories", ",".join(params.get("categories", [])),
                "--whitelist", path_join(root, "basic_tools", "flaggems", "flaggems_pass_whitelist.tsv"),
                "--full-list", path_join(root, "basic_tools", "flaggems", "flaggems_all_ops.tsv"),
                "--flaggems-dir", fg, "--python-bin", py, "--artifact-dir", out,
                "--selected-output", path_join(out, "selected.txt"), "--clear-cache", "0",
                "--pytest-args=" + config.get("flaggems_pytest_args", "--ref cpu -vs")]
        add(argv, fg, 86400 if mode == "full" else 14400)
    else:
        kernels = params.get("kernels", ["add", "mm", "softmax", "layernorm"])
        if not isinstance(kernels, list) or not kernels or not set(kernels) <= {"add", "mm", "softmax", "layernorm"}:
            raise ValueError("kernels must select supported benchmark kernels")
        repeats = bounded(params.get("repeat", 20 if tool_id == "ir_serialization" else 3), "repeat", 1, 100)
        warmup = bounded(params.get("warmup", 1), "warmup", 0, 20)
        backend = config.get("expected_backend")
        fg = config.get("flaggems_dir")
        if not backend or not fg:
            raise ValueError("Performance tools require expected_backend and flaggems_dir")
        script = {"compile_time": "compile_benchmark", "pass_profile": "pass_profile_benchmark", "ir_serialization": "ir_serialization_benchmark"}[tool_id]
        argv = [py, path_join(root, "basic_tools", "performance", script + ".py"), "--backend", backend,
                "--flaggems-root", fg, "--kernels", ",".join(kernels), "--repeat", str(repeats), "--warmup", str(warmup),
                "--output-json", path_join(out, "candidate.json")]
        if tool_id == "pass_profile":
            for flag, name in (("--output-events-csv", "events.csv"), ("--output-summary-csv", "summary.csv"), ("--output-hotspots-markdown", "hotspots.md")):
                argv += [flag, path_join(out, name)]
        else:
            argv += ["--output-csv", path_join(out, "candidate.csv")]
        if tool_id == "ir_serialization":
            argv += ["--output-markdown", path_join(out, "serialization.md"), "--work-root", path_join(out, "work")]
        else:
            argv += ["--cache-root", path_join(out, "cache"), "--dump-root", path_join(out, "dump")]
        if config.get("vendor"):
            argv += ["--vendor", config["vendor"]]
        add(argv, fg, 7200)
        action("compare_performance", {"kernels": kernels}, timeout=120)
    result["artifacts"] = [out]
    return result


def execute(tool_id: str, context: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Diagnostic executor; these results are explicitly not broker receipts."""
    spec = plan(tool_id, context, parameters)
    if spec["status"] != "ready":
        return spec
    records = []
    for command in spec["commands"]:
        start = time.monotonic()
        try:
            completed = subprocess.run(command["argv"], cwd=command["cwd"],
                                       env={**os.environ, **command["env"]}, timeout=command["timeout"], check=False)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = 124
        records.append({"argv": command["argv"], "returncode": code, "duration_seconds": time.monotonic() - start})
        if code:
            break
    return {**spec, "status": "pass" if records and records[-1]["returncode"] == 0 else "fail",
            "execution": records, "trusted_receipt": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool_id", choices=TOOL_IDS)
    parser.add_argument("--context", required=True, help="Controller context JSON file")
    parser.add_argument("--parameters", default="{}", help="Agent parameter JSON object")
    parser.add_argument("--execute", action="store_true", help="Run diagnostically in the current persistent environment")
    args = parser.parse_args()
    context = json.loads(Path(args.context).read_text(encoding="utf-8-sig"))
    result = (execute if args.execute else plan)(args.tool_id, context, json.loads(args.parameters))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
