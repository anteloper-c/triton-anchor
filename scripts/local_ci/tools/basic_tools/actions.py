#!/usr/bin/env python3
"""Container-side preconditions and artifact operations for the basic tools.

This file is loaded from the controller's read-only tool mount, not the PR.
Artifacts here prove build dependencies; only the external broker attests commands.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    print("$ " + repr(argv), flush=True)
    return subprocess.run(argv, check=True, **kwargs)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def candidate_python(context: dict[str, Any]) -> str:
    """The host-selected task wrapper, never this trusted helper interpreter."""
    return context.get("python_bin", sys.executable)


def remove_child(root: Path, child: Path) -> None:
    """Bound rebuild cleanup to known direct children and reject linked paths."""
    root = root.resolve(strict=True)
    if child.is_symlink() or child.parent.resolve(strict=True) != root:
        raise ValueError(f"Refusing unsafe build cleanup: {child}")
    resolved = child.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Refusing cleanup outside build source: {child}")
    if child.is_dir():
        shutil.rmtree(child)
    elif child.exists():
        child.unlink()


def wheel_manifest(context: dict[str, Any], build_tool: str) -> tuple[dict[str, Any], Path]:
    root = Path(context["artifact_dir"]) / build_tool
    manifest = read_json(root / "wheel.json")
    if manifest.get("target_sha") != context["target_sha"] or manifest.get("task_id") != context["task_id"]:
        raise ValueError("Wheel artifact belongs to a different tested commit/task")
    wheel = Path(manifest["wheel"])
    if not wheel.is_file() or wheel.is_symlink() or root.resolve() not in wheel.resolve().parents:
        raise ValueError("Wheel path is missing or outside the current task build artifact directory")
    if digest(wheel) != manifest["sha256"]:
        raise ValueError("Wheel artifact hash mismatch; rebuild before continuing")
    return manifest, wheel


def require_installation(context: dict[str, Any], build_tool: str, install_tool: str) -> None:
    manifest, _ = wheel_manifest(context, build_tool)
    installed = read_json(Path(context["artifact_dir"]) / install_tool / "installation.json")
    for key in ("task_id", "target_sha", "sha256"):
        if installed.get(key) != manifest.get(key):
            raise ValueError(f"Installed wheel no longer matches current {build_tool}; reinstall before continuing")
    if Path(installed.get("python_executable", "")).resolve() != Path(candidate_python(context)).resolve():
        raise ValueError("Installed wheel belongs to a different Python environment")
    if installed.get("task_venv") != context.get("task_venv"):
        raise ValueError("Installed wheel belongs to a different task venv")


def preflight(payload: dict[str, Any]) -> None:
    context, tool = payload["context"], payload["tool_id"]
    source = Path(context["source_dir"])
    if not source.is_dir():
        raise ValueError(f"Source checkout unavailable: {source}")
    actual = run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if actual != context["target_sha"]:
        raise ValueError(f"Checkout HEAD does not match task target: {actual}")
    artifact = Path(context["artifact_dir"])
    if artifact.resolve() == source.resolve() or source.resolve() in artifact.resolve().parents:
        raise ValueError("Artifacts must be outside the candidate source checkout")
    out = artifact / tool
    for directory in (out, out / "tmp", out / "cache", out / "dump"):
        directory.mkdir(parents=True, exist_ok=True)
    if tool in {"wheel_install", "frontend_smoke", "backend_rebuild"}:
        wheel_manifest(context, "frontend_build")
    if tool in {"frontend_smoke", "backend_rebuild"}:
        require_installation(context, "frontend_build", "wheel_install")
    if tool in {"backend_smoke", "flaggems", "compile_time", "pass_profile", "ir_serialization"}:
        require_installation(context, "frontend_build", "wheel_install")
        require_installation(context, "backend_rebuild", "backend_rebuild")
    print(json.dumps({"task_id": context["task_id"], "target_sha": actual, "tool": tool}))


def environment(payload: dict[str, Any]) -> None:
    context = payload["context"]
    config = context.get("profile", {}).get("tools", {})
    missing = []
    commands = {}
    for executable in config.get("required_commands", ["git", "cmake", "ninja"]):
        found = shutil.which(executable)
        commands[executable] = found
        if not found:
            missing.append(f"executable:{executable}")
    requested_modules = config.get("required_modules", ["build", "setuptools", "wheel", "pybind11"])
    probe = run([candidate_python(context), "-I", "-c",
                 "import importlib.util,json,sys; "
                 "print(json.dumps({'python':sys.version,'python_executable':sys.executable,"
                 "'modules':{name:importlib.util.find_spec(name) is not None for name in json.loads(sys.argv[1])}}))",
                 json.dumps(requested_modules)], capture_output=True, text=True)
    observed = json.loads(probe.stdout)
    modules = observed["modules"]
    for module, present in modules.items():
        if not present:
            missing.append(f"python-module:{module}")
    source = Path(context["source_dir"])
    for name in config.get("required_source_files", ["setup.py", "pyproject.toml", "tests/test_smoke.py"]):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("required_source_files must stay within the checkout")
        if not (source / relative).is_file():
            missing.append(f"source:{name}")
    llvm_version = None
    if config.get("llvm_dir"):
        llvm = Path(config["llvm_dir"])
        for name in ("include", "lib", "bin"):
            if not (llvm / name).is_dir():
                missing.append(f"LLVM:{llvm / name}")
        llvm_config = llvm / "bin" / ("llvm-config.exe" if os.name == "nt" else "llvm-config")
        if llvm_config.is_file():
            llvm_version = run([str(llvm_config), "--version"], text=True, capture_output=True).stdout.strip()
        else:
            missing.append(f"LLVM:{llvm_config}")
    result = {"python": observed["python"], "python_executable": observed["python_executable"], "commands": commands,
              "modules": modules, "llvm_version": llvm_version,
              "profile_llvm_revision": context.get("profile", {}).get("llvm_revision"),
              "missing": missing, "target_sha": context["target_sha"], "task_id": context["task_id"]}
    write_json(Path(context["artifact_dir"]) / "environment" / "environment.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if missing:
        raise RuntimeError("Environment prerequisites missing: " + ", ".join(missing))
    # A successful import discovery does not establish a consistent dependency set.
    run([candidate_python(context), "-m", "pip", "check"])


def prepare_build(payload: dict[str, Any]) -> None:
    source = Path(payload["build_source"])
    if not source.is_dir() or not any((source / name).is_file() for name in ("pyproject.toml", "setup.py")):
        raise ValueError(f"Build source is not an installable Python project: {source}")
    if payload["parameters"].get("build_mode", "fresh") == "fresh":
        remove_child(source, source / "build")
    remove_child(source, source / "dist")
    for child in source.glob("*.egg-info"):
        remove_child(source, child)
    out = Path(payload["context"]["artifact_dir"]) / payload["tool_id"]
    remove_child(out, out / "wheels")
    (out / "wheels").mkdir()
    # Invalidate the previous attempt before any new build starts.
    for name in ("wheel.json", "installation.json"):
        (out / name).unlink(missing_ok=True)


def record_wheel(payload: dict[str, Any]) -> None:
    context, tool = payload["context"], payload["tool_id"]
    out = Path(context["artifact_dir"]) / tool
    pattern = "triton_anchor-*.whl" if tool == "frontend_build" else context["profile"]["tools"].get("backend_wheel_pattern")
    if not pattern or "/" in pattern or "\\" in pattern:
        raise ValueError("A filename-only backend_wheel_pattern is required")
    wheels = list((out / "wheels").glob(pattern))
    if len(wheels) != 1:
        raise ValueError(f"Expected exactly one new wheel matching {pattern}; found {len(wheels)}")
    wheel = wheels[0]
    if wheel.is_symlink():
        raise ValueError("Build returned a symlink instead of a wheel")
    write_json(out / "wheel.json", {"wheel": str(wheel.resolve()), "sha256": digest(wheel),
                                     "target_sha": context["target_sha"], "task_id": context["task_id"]})
    print(f"Built wheel: {wheel.name}; sha256={digest(wheel)}")


def install_wheel(payload: dict[str, Any]) -> None:
    context, tool = payload["context"], payload["tool_id"]
    build_tool = "backend_rebuild" if tool == "backend_rebuild" else "frontend_build"
    manifest, wheel = wheel_manifest(context, build_tool)
    # Dependencies belong to the daily trusted environment recipe. Installing the
    # candidate must not silently replace the matched LLVM/Triton/backend stack.
    run([candidate_python(context), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)])
    write_json(Path(context["artifact_dir"]) / tool / "installation.json",
               {**manifest, "python_executable": candidate_python(context), "task_venv": context.get("task_venv")})


def backend_discovery(payload: dict[str, Any]) -> None:
    context = payload["context"]
    expected = context.get("profile", {}).get("tools", {}).get("expected_backend")
    if not expected:
        raise ValueError("profile.tools.expected_backend is required")
    # Pass the name as argv data; no Python code interpolation.
    run([candidate_python(context), "-I", "-c",
         "import sys; from triton.backends import backends; print(sorted(backends)); "
         "assert sys.argv[1] in backends, 'Expected backend not discovered'", expected])


def smoke_success(payload: dict[str, Any]) -> None:
    """A trusted post-step marker; the broker still requires the preceding receipt."""
    context, tool = payload["context"], payload["tool_id"]
    write_json(Path(context["artifact_dir"]) / tool / "smoke_success.json",
               {"task_id": context["task_id"], "target_sha": context["target_sha"],
                "python_executable": candidate_python(context), "task_venv": context.get("task_venv"),
                "tool": tool, "status": "passed"})


def compare_performance(payload: dict[str, Any]) -> None:
    context, tool = payload["context"], payload["tool_id"]
    out = Path(context["artifact_dir"]) / tool
    baseline = context.get("performance_baselines", {}).get(tool)
    baseline_path = None
    profile = context.get("profile", {})
    unavailable = "没有与基准提交及当前环境匹配的受信基线；已测候选版本，性能变化尚未验证。"
    if baseline:
        if baseline.get("base_sha") != context.get("base_sha") or not re.fullmatch(r"[0-9a-f]{40}", baseline.get("base_sha", "")):
            raise ValueError("Performance baseline is not for the task base commit")
        for key, expected in (("profile_id", profile.get("id")), ("llvm_revision", profile.get("llvm_revision"))):
            if not expected or baseline.get(key) != expected:
                raise ValueError(f"Performance baseline {key} differs from current trusted environment")
        baseline_path = Path(baseline["path"])
        if not baseline_path.is_file() or digest(baseline_path) != baseline.get("sha256"):
            raise ValueError("Performance baseline missing or SHA-256 differs from trusted manifest")
        unavailable = ""
    script = {"compile_time": "compare_compile_time.py", "pass_profile": "compare_pass_profile.py", "ir_serialization": "compare_ir_serialization.py"}[tool]
    command = [sys.executable, "-I", "-S", str(Path(__file__).parent / "performance" / script),
               "--candidate-json", str(out / "candidate.json"), "--candidate-sha", context["target_sha"],
               "--base-sha", context.get("base_sha", ""), "--kernels", ",".join(payload["kernels"]),
               "--output-json", str(out / "comparison.json"), "--output-markdown", str(out / "comparison.md")]
    if baseline_path:
        command += ["--baseline-json", str(baseline_path)]
    if tool != "compile_time":
        command += ["--output-csv", str(out / "comparison.csv")]
    run(command)
    write_json(out / "baseline_identity.json", {"baseline_available": baseline_path is not None,
               "baseline": baseline, "reason": unavailable, "profile_id": profile.get("id"),
               "llvm_revision": profile.get("llvm_revision"), "target_sha": context["target_sha"],
               "regression_blocks_merge": False})
    if unavailable:
        print(unavailable)


ACTIONS = {function.__name__: function for function in (preflight, environment, prepare_build, record_wheel,
           install_wheel, backend_discovery, smoke_success, compare_performance)}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ACTIONS:
        raise SystemExit("usage: actions.py <action> <JSON payload>")
    ACTIONS[sys.argv[1]](json.loads(sys.argv[2]))
