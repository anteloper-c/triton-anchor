#!/usr/bin/env python3
"""Independent, real Local CI tools. Scheduling and authorization live outside this module."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PERFORMANCE = ROOT / "deterministic_ci/performance"
FLAGGEMS = ROOT / "deterministic_ci/flaggems"
TOOL_IDS = (
    "environment", "frontend_build", "wheel_install_import", "frontend_smoke",
    "backend_rebuild", "backend_smoke_jit", "flaggems", "compile_time",
    "pass_profile", "ir_serialization", "contract_tests",
)
BACKEND_TOOLS = set(TOOL_IDS[4:10])
SUCCESSORS = {
    "frontend_build": TOOL_IDS[2:10],
    "wheel_install_import": TOOL_IDS[3:10],
    "backend_rebuild": TOOL_IDS[5:10],
}


class ToolError(RuntimeError):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ToolError(f"Refusing symlinked output: {path}", 2)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ToolError(f"Expected a JSON object: {path}")
    return value


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def absolute_directory(value: str, name: str, *, create: bool = False) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or path.is_symlink():
        raise ToolError(f"{name} must be an absolute, non-symlink directory", 2)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    result = path.resolve(strict=True)
    if not result.is_dir() or result == Path("/"):
        raise ToolError(f"Invalid {name}: {result}", 2)
    return result


def clear_build_outputs(root: Path, *, fresh: bool = True) -> None:
    """Delete only direct build outputs, never a checkout or external target."""
    targets = [root / "dist", *root.glob("*.egg-info")]
    if fresh:
        targets.append(root / "build")
    for path in targets:
        if path.parent != root or path.is_symlink():
            raise ToolError(f"Refusing unsafe build output: {path}", 2)
        if path.exists() and path.resolve().parent != root:
            raise ToolError(f"Build output escaped checkout: {path}", 2)
        if path.is_dir():
            if os.path.ismount(path):
                raise ToolError(f"Refusing mounted build output: {path}", 2)
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


class Context:
    def __init__(self, tool: str, environ: dict[str, str] | None = None):
        self.tool = tool
        self.env = dict(os.environ if environ is None else environ)
        self.artifacts = absolute_directory(self.env.get("LOCAL_CI_ARTIFACT_DIR", ""), "LOCAL_CI_ARTIFACT_DIR", create=True)
        self.result_path = Path(self.env.get("LOCAL_CI_TOOL_RESULT", str(self.artifacts / "result.json")))
        if not self.result_path.is_absolute() or self.result_path.resolve().parent != self.artifacts:
            raise ToolError("LOCAL_CI_TOOL_RESULT must be a file directly inside LOCAL_CI_ARTIFACT_DIR", 2)
        self.task = absolute_directory(self.env.get("LOCAL_CI_TASK_ROOT", ""), "LOCAL_CI_TASK_ROOT", create=True)
        self.state = absolute_directory(str(self.task / "state"), "task state", create=True)
        if self.state.parent != self.task:
            raise ToolError("State directory escaped task root", 2)
        self.anchor = absolute_directory(self.env.get("ANCHOR_DIR", ""), "ANCHOR_DIR")
        self.commands: list[dict[str, Any]] = []
        self.details: dict[str, Any] = {}
        self.started = time.time()
        self.sha = ""
        self.fingerprint = self.env.get("LOCAL_CI_ENVIRONMENT_FINGERPRINT", "")
        self.env.setdefault("MAX_JOBS", "8")
        self.env.setdefault("CMAKE_BUILD_PARALLEL_LEVEL", self.env["MAX_JOBS"])
        self.env.setdefault("NINJAFLAGS", "-j" + self.env["MAX_JOBS"])
        self.env.setdefault("UV_LINK_MODE", "copy")
        for name in ("MAX_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL"):
            if not self.env[name].isdigit() or int(self.env[name]) < 1:
                raise ToolError(f"{name} must be positive", 2)
        invocation = tool + "-" + hashlib.sha256(str(self.artifacts).encode()).hexdigest()[:16]
        temporary = absolute_directory(str(self.task / "tmp" / invocation), "tool temporary directory", create=True)
        if not temporary.is_relative_to(self.task):
            raise ToolError("Temporary directory escaped task root", 2)
        self.env["TMPDIR"] = str(temporary)
        self.env["TRITON_DUMP_DIR"] = str(temporary / "dump")
        self.env["TRITON_CACHE_DIR"] = str(temporary / "triton-cache")
        Path(self.env["TRITON_DUMP_DIR"]).mkdir(exist_ok=True)
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        # Installed-wheel checks must never inherit a candidate source PYTHONPATH.
        self.env.pop("PYTHONPATH", None)

    @property
    def python(self) -> str:
        return self.env.get("PYTHON_BIN", "python3")

    def run(self, args: list[str], *, cwd: Path | None = None, capture: bool = False, timeout: int | None = None) -> str:
        command = [str(arg) for arg in args]
        log = self.artifacts / f"command-{len(self.commands) + 1:02d}.log"
        record: dict[str, Any] = {"argv": command, "cwd": str(cwd or self.anchor), "log": log.name, "started_at": time.time()}
        self.commands.append(record)
        print(f"[{self.tool}] {shlex.join(command)}", flush=True)
        limit = timeout or int(self.env.get("LOCAL_CI_TOOL_TIMEOUT_SECONDS", "7200"))
        process = None
        try:
            with log.open("wb") as stream:
                process = subprocess.Popen(command, cwd=cwd or self.anchor, env=self.env,
                                           stdout=subprocess.PIPE if capture else stream,
                                           stderr=stream if capture else subprocess.STDOUT, start_new_session=True)
                output, _ = process.communicate(timeout=limit)
            record["exit_code"] = process.returncode
            if process.returncode:
                raise ToolError(f"Command failed with exit {process.returncode}; see {log.name}", process.returncode if process.returncode > 0 else 128 - process.returncode)
            if capture:
                decoded = (output or b"").decode("utf-8", errors="strict")
                log.write_text(log.read_text(encoding="utf-8", errors="replace") + decoded, encoding="utf-8")
                return decoded
            return ""
        except subprocess.TimeoutExpired as exc:
            record["exit_code"] = 124
            raise ToolError(f"Command timed out after {limit}s; see {log.name}", 124) from exc
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            record["finished_at"] = time.time()

    def source(self, path: Path, args: list[str] | None = None) -> None:
        if not path.is_file():
            raise ToolError(f"Environment script is missing: {path}", 2)
        # env -0 is parsed in memory; never log a complete environment or secrets.
        log = self.artifacts / f"environment-{len(self.commands) + 1:02d}.log"
        command = ["bash", "--noprofile", "--norc", "-c", 'set -e; source "$1" "${@:2}" >&2; env -0', "environment", str(path), *(args or [])]
        with log.open("wb") as stream:
            completed = subprocess.run(command, cwd=self.anchor, env=self.env, stdout=subprocess.PIPE, stderr=stream, timeout=60)
        self.commands.append({"argv": ["source", str(path), *(args or [])], "cwd": str(self.anchor), "exit_code": completed.returncode, "log": log.name})
        if completed.returncode:
            raise ToolError(f"Environment setup failed: {path}", completed.returncode)
        loaded = dict(item.decode("utf-8").split("=", 1) for item in completed.stdout.split(b"\0") if item)
        # Trusted envsetup sometimes resets these to shared global directories.
        preserved = {"TMPDIR", "TRITON_DUMP_DIR", "TRITON_CACHE_DIR", "MAX_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL", "NINJAFLAGS",
                     "ANCHOR_DIR", "BACKEND_PATH", "PYTHON_BIN", "PYTHON_VENV_ACTIVATE", "HOME", "BASELINE_JSON"}
        preserved.update(key for key in self.env if key.startswith("LOCAL_CI_"))
        for key in preserved:
            if key in self.env:
                loaded[key] = self.env[key]
        if self.env.get("PYTHON_BIN") and Path(self.env["PYTHON_BIN"]).is_absolute():
            python_bin = Path(self.env["PYTHON_BIN"])
            loaded["PATH"] = str(python_bin.parent) + os.pathsep + loaded.get("PATH", "")
            loaded["VIRTUAL_ENV"] = str(python_bin.parent.parent)
        # Preserve backend dependency paths, but never import product code from
        # the candidate source tree instead of the installed wheel.
        loaded["PYTHONPATH"] = os.pathsep.join(
            entry for entry in loaded.get("PYTHONPATH", "").split(os.pathsep)
            if entry and Path(entry).is_absolute() and not Path(entry).resolve().is_relative_to(self.anchor)
        )
        self.env = loaded

    def prepare(self) -> None:
        self.sha = self.run(["git", "rev-parse", "HEAD"], capture=True).strip()
        expected = next((self.env[key] for key in ("LOCAL_CI_TESTED_SHA", "LOCAL_CI_TARGET_SHA", "GITHUB_SHA") if self.env.get(key)), self.sha)
        if not re.fullmatch(r"[0-9a-f]{40}", self.sha) or self.sha != expected:
            raise ToolError("Checkout SHA does not match tested SHA", 2)
        self.env["GITHUB_SHA"] = self.sha
        if self.env.get("PYTHON_VENV_ACTIVATE"):
            self.source(Path(self.env["PYTHON_VENV_ACTIVATE"]))
        if self.env.get("TRUSTED_ANCHOR_ENVSETUP"):
            self.source(Path(self.env["TRUSTED_ANCHOR_ENVSETUP"]))
        if self.env.get("LLVM_BUILD_DIR"):
            llvm = Path(self.env["LLVM_BUILD_DIR"])
            self.env.setdefault("LLVM_SYSPATH", str(llvm))
            self.env.setdefault("LLVM_INCLUDE_DIRS", str(llvm / "include"))
            self.env.setdefault("LLVM_LIBRARY_DIR", str(llvm / "lib"))
            self.env.setdefault("LLVM_BINARY_DIR", str(llvm / "bin"))
        if self.tool in BACKEND_TOOLS:
            if self.env.get("RUN_BACKEND_STAGES") != "true":
                raise ToolError("Backend capability is not enabled by the trusted profile", 2)
            self.backend = absolute_directory(self.env.get("BACKEND_PATH", ""), "BACKEND_PATH")
            if self.backend == self.anchor or self.backend.is_relative_to(self.anchor) or self.anchor.is_relative_to(self.backend):
                raise ToolError("Backend and frontend checkouts must not overlap", 2)
            setup = self.env.get("BACKEND_ENVSETUP", "")
            if setup:
                path = Path(setup)
                self.source(path if path.is_absolute() else self.backend / path, shlex.split(self.env.get("BACKEND_ENVSETUP_ARGS", "")))
        if not self.fingerprint and (self.state / "environment-manifest.json").is_file():
            saved = read_json(self.state / "environment-manifest.json")
            if saved.get("tested_sha") == self.sha:
                self.fingerprint = str(saved.get("fingerprint", ""))

    def package_command(self, operation: str, *args: str) -> list[str]:
        package_tool = self.env.get("PACKAGE_TOOL", "auto")
        if package_tool not in {"auto", "pip", "uv"}:
            raise ToolError("PACKAGE_TOOL must be auto, pip or uv", 2)
        if package_tool == "uv" or (package_tool == "auto" and shutil.which("uv", path=self.env.get("PATH"))):
            return ["uv", "pip", operation, "--python", self.python, *args]
        return [self.python, "-m", "pip", operation, *args]

    def wheel(self, kind: str) -> Path:
        value = read_json(self.state / f"{kind}-wheel.json")
        path = Path(value.get("path", ""))
        if value.get("tested_sha") != self.sha or not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ToolError(f"Missing/stale {kind} wheel state")
        expected_root = self.anchor if kind == "frontend" else self.backend
        if path.resolve().parent != expected_root / "dist" or digest(path) != value.get("sha256"):
            raise ToolError(f"{kind} wheel path/hash does not match this task")
        if self.fingerprint and value.get("fingerprint") != self.fingerprint:
            raise ToolError(f"{kind} wheel was built in a different environment")
        return path

    def build_wheel(self, kind: str, checkout: Path, pattern: str) -> Path:
        state_file = self.state / f"{kind}-wheel.json"
        state_file.unlink(missing_ok=True)
        fresh = kind == "backend" or self.env.get("FRONTEND_BUILD_MODE", "fresh") == "fresh"
        if kind == "frontend" and self.env.get("FRONTEND_BUILD_MODE", "fresh") not in {"fresh", "incremental"}:
            raise ToolError("FRONTEND_BUILD_MODE must be fresh or incremental", 2)
        clear_build_outputs(checkout, fresh=fresh)
        uv = self.package_command("install")[0] == "uv"
        command = ["uv", "build", "--wheel", "--no-build-isolation", "--python", self.python] if uv else [self.python, "-m", "build", "--wheel", "--no-isolation"]
        self.run(command, cwd=checkout)
        wheels = sorted((checkout / "dist").glob(pattern))
        if len(wheels) != 1 or not wheels[0].is_file() or wheels[0].is_symlink():
            raise ToolError(f"Expected exactly one {kind} wheel matching {pattern!r}; found {len(wheels)}")
        wheel = wheels[0].resolve()
        value = {"path": str(wheel), "sha256": digest(wheel), "tested_sha": self.sha, "fingerprint": self.fingerprint}
        write_json(state_file, value)
        self.details["wheel"] = value
        return wheel

    def finish(self, code: int, error: str = "") -> None:
        result = {"schema": "triton-anchor-local-ci-tool/v1", "tool_id": self.tool,
                  "status": "pass" if code == 0 else "cancelled" if code in (130, 143) else "fail",
                  "exit_code": code, "tested_sha": self.sha, "environment_fingerprint": self.fingerprint,
                  "started_at": self.started, "finished_at": time.time(), "commands": self.commands,
                  "details": self.details, "error": error}
        write_json(self.result_path, result)
        if code == 0:
            write_json(self.state / f"{self.tool}.json", result)


def environment(ctx: Context) -> None:
    llvm = absolute_directory(ctx.env.get("LLVM_BUILD_DIR", ""), "LLVM_BUILD_DIR")
    for path in (llvm / "bin/llvm-config", llvm / "include/llvm", llvm / "include/mlir", llvm / "lib"):
        if not path.exists():
            raise ToolError(f"Required LLVM dependency is missing: {path}", 2)
    configured_hash = ctx.env.get("LOCAL_CI_LLVM_HASH", "")
    llvm_hash_file = ctx.anchor / "triton/cmake/llvm-hash.txt"
    if not re.fullmatch(r"[0-9a-f]{40}", configured_hash) or llvm_hash_file.read_text().strip() != configured_hash:
        raise ToolError("Trusted profile LLVM hash does not match candidate llvm-hash.txt", 2)
    manifest: dict[str, Any] = {"llvm_hash": configured_hash, "llvm_version": ctx.run([str(llvm / "bin/llvm-config"), "--version"], capture=True).strip(),
                              "cmake": ctx.run(["cmake", "--version"], capture=True).splitlines()[0],
                              "ninja": ctx.run(["ninja", "--version"], capture=True).strip(),
                              "cxx": ctx.run([*shlex.split(ctx.env.get("CXX", "c++")), "--version"], capture=True).splitlines()[0],
                              "profile": ctx.env.get("LOCAL_CI_PROFILE_NAME", ""), "backend_enabled": ctx.env.get("RUN_BACKEND_STAGES") == "true"}
    manifest["python"] = json.loads(ctx.run([ctx.python, "-c", "import importlib.metadata as m,json,sys; print(json.dumps({'executable':sys.executable,'version':sys.version,'packages':{p:m.version(p) for p in ['build','setuptools','wheel','pybind11']}}))"], capture=True))
    ctx.run(ctx.package_command("check"))
    if manifest["backend_enabled"]:
        for name in ("BACKEND_PATH", "FLAGGEMS_CLONE_DIR", "PPL_ROOT"):
            absolute_directory(ctx.env.get(name, ""), name)
        if not ctx.env.get("EXPECTED_TRITON_BACKEND") or not ctx.env.get("BACKEND_TEST_COMMAND"):
            raise ToolError("Backend profile requires discovery name and smoke/JIT command", 2)
        for name in ("BACKEND_PATH", "FLAGGEMS_CLONE_DIR"):
            manifest[name.lower() + "_sha"] = ctx.run(["git", "rev-parse", "HEAD"], cwd=Path(ctx.env[name]), capture=True).strip()
    minimum = int(ctx.env.get("LOCAL_CI_MIN_FREE_BYTES", str(5 * 1024**3)))
    if minimum < 0:
        raise ToolError("LOCAL_CI_MIN_FREE_BYTES must be non-negative", 2)
    free = min(shutil.disk_usage(ctx.task).free, shutil.disk_usage(ctx.anchor).free)
    if free < minimum:
        raise ToolError(f"Insufficient free space: {free} bytes, require {minimum}", 2)
    calculated = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    ctx.fingerprint = ctx.fingerprint or calculated
    ctx.details.update({"environment": manifest, "free_bytes": free})
    write_json(ctx.state / "environment-manifest.json", {"tested_sha": ctx.sha, "fingerprint": ctx.fingerprint, "manifest": manifest})
    write_json(ctx.artifacts / "environment.json", ctx.details)


def frontend_build(ctx: Context) -> None:
    ctx.build_wheel("frontend", ctx.anchor, "triton_anchor-*.whl")


def wheel_install_import(ctx: Context) -> None:
    wheel = ctx.wheel("frontend")
    ctx.run(ctx.package_command("install", "--force-reinstall", "--no-deps", str(wheel)))
    neutral = Path(ctx.env["TMPDIR"])
    code = """import os
report_fd=os.dup(1)
os.dup2(2,1)
import importlib.metadata as m,json,pathlib,triton,triton_anchor
d=m.distribution('triton-anchor')
files={pathlib.Path(d.locate_file(f)).resolve() for f in d.files or []}
paths={name:pathlib.Path(module.__file__).resolve() for name,module in [('triton',triton),('triton_anchor',triton_anchor)]}
assert all(path in files for path in paths.values()), 'Imports did not originate from the installed wheel'
os.write(report_fd,(json.dumps({'distribution_version':d.version,'imports':{k:str(v) for k,v in paths.items()}})+'\\n').encode())
os.close(report_fd)
"""
    ctx.details["installation"] = json.loads(ctx.run([ctx.python, "-I", "-c", code], cwd=neutral, capture=True))
    ctx.details["wheel"] = read_json(ctx.state / "frontend-wheel.json")


def frontend_smoke(ctx: Context) -> None:
    ctx.wheel("frontend")
    ctx.run([ctx.python, "-I", str(ctx.anchor / "tests/test_smoke.py")], cwd=Path(ctx.env["TMPDIR"]))


def backend_rebuild(ctx: Context) -> None:
    pattern = ctx.env.get("BACKEND_WHEEL_PATTERN", "")
    if not pattern or "/" in pattern or "\\" in pattern:
        raise ToolError("BACKEND_WHEEL_PATTERN must be a filename pattern", 2)
    ctx.run(ctx.package_command("install", "scikit-build-core", "pybind11", "build"))
    wheel = ctx.build_wheel("backend", ctx.backend, pattern)
    ctx.run(ctx.package_command("install", "--force-reinstall", "--no-deps", str(wheel)))
    ctx.run([ctx.python, "-I", "-c", "import os; from triton.backends import backends; expected=os.environ['EXPECTED_TRITON_BACKEND']; assert expected in backends,(expected,list(backends)); print('backend discovered:',expected)"], cwd=Path(ctx.env["TMPDIR"]))


def backend_smoke_jit(ctx: Context) -> None:
    ctx.wheel("backend")
    command = ctx.env.get("BACKEND_TEST_COMMAND", "")
    if not command.strip():
        raise ToolError("BACKEND_TEST_COMMAND is required", 2)
    ctx.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", command], cwd=ctx.backend)


def flaggems(ctx: Context) -> None:
    seed = ctx.env.get("FLAGGEMS_RANDOM_SEED", "")
    if not seed:
        raise ToolError("FLAGGEMS_RANDOM_SEED must be supplied for reproducible selection", 2)
    ctx.run([ctx.python, str(FLAGGEMS / "batch_test_flaggems.py"),
             "--mode", ctx.env.get("FLAGGEMS_TEST_MODE", "sample"),
             "--sample-size", ctx.env.get("FLAGGEMS_SAMPLE_SIZE", "6"), "--seed", seed,
             "--op", ctx.env.get("FLAGGEMS_TEST_OP", ""), "--affected-ops", ctx.env.get("FLAGGEMS_AFFECTED_OPS", ""),
             "--whitelist", ctx.env.get("FLAGGEMS_WHITELIST") or str(FLAGGEMS / "flaggems_pass_whitelist.tsv"),
             "--full-list", ctx.env.get("FLAGGEMS_FULL_LIST") or str(FLAGGEMS / "flaggems_all_ops.tsv"),
             "--flaggems-dir", ctx.env["FLAGGEMS_CLONE_DIR"], "--python-bin", ctx.python,
             "--artifact-dir", str(ctx.artifacts), "--selected-output", str(ctx.artifacts / "flaggems-selected.txt"),
             "--pytest-args=" + ctx.env.get("FLAGGEMS_PYTEST_ARGS", "--ref cpu -vs"),
             "--idle-timeout-seconds", ctx.env.get("FLAGGEMS_IDLE_TIMEOUT_SECONDS", "300"),
             "--total-timeout-seconds", ctx.env.get("FLAGGEMS_TOTAL_TIMEOUT_SECONDS", "6000"),
             "--full-hard-timeout-seconds", ctx.env.get("FLAGGEMS_FULL_HARD_TIMEOUT_SECONDS", "14400"),
             "--clear-cache", "1"], cwd=ctx.backend)
    summary = read_json(ctx.artifacts / "flaggems-summary.json")
    counts = summary.get("summary", {})
    if counts.get("status") != "pass" or int(counts.get("passed", 0)) < 1:
        raise ToolError("FlagGems did not execute a nonempty passing test selection")
    ctx.details["flaggems"] = summary


def validate_measurements(tool: str, candidate: dict[str, Any], kernels: list[str]) -> None:
    if not kernels or not isinstance(candidate.get("summary"), dict):
        raise ToolError("Benchmark produced no valid candidate summary")
    for kernel in kernels:
        item = candidate["summary"].get(kernel, {})
        if tool == "compile_time":
            measurements = [item.get("compile_est", {})]
            if item.get("all_correct") is not True:
                raise ToolError(f"Compile benchmark correctness failed/missing for {kernel}")
        elif tool == "pass_profile":
            passes = item.get("passes", {})
            if not passes or not any(event.get("kind") == "pass" and event.get("kernel") == kernel for event in candidate.get("events", [])):
                raise ToolError(f"No pass timing events collected for {kernel}")
            measurements = [value.get("wall_ms", {}) for value in passes.values()]
        else:
            if item.get("module_count", 0) < 1:
                raise ToolError(f"No IR modules collected for {kernel}")
            measurements = [item.get("metrics", {}).get(metric, {}) for metric in ("serialize", "deserialize", "roundtrip")]
            rows = [row for row in candidate.get("raw", []) if row.get("kernel") == kernel]
            if not rows or not all(row.get("roundtrip_verified") is True for row in rows):
                raise ToolError(f"IR roundtrip was not verified for {kernel}")
        for value in measurements:
            median = value.get("median_ms")
            if isinstance(median, bool) or not isinstance(median, (int, float)) or not math.isfinite(median) or median < 0 or value.get("count", 0) < 1:
                raise ToolError(f"Invalid/empty measured candidate timing for {kernel}")


def performance(ctx: Context) -> None:
    tool = ctx.tool
    stem, prefix, default_repeat, default_warmup = {
        "compile_time": ("compile_benchmark", "COMPILE_BENCHMARK", "5", "1"),
        "pass_profile": ("pass_profile_benchmark", "PASS_PROFILE", "3", "1"),
        "ir_serialization": ("ir_serialization_benchmark", "IR_SERIALIZATION", "20", "3"),
    }[tool]
    kernels_text = ctx.env.get(prefix + "_KERNELS", "add,mm,softmax,layernorm")
    kernels = [item.strip() for item in kernels_text.split(",") if item.strip()]
    output = ctx.artifacts / "candidate.json"
    arguments = [ctx.python, str(PERFORMANCE / (stem + ".py")), "--backend", ctx.env["EXPECTED_TRITON_BACKEND"],
                 "--vendor", ctx.env["EXPECTED_TRITON_BACKEND"], "--flaggems-root", ctx.env["FLAGGEMS_CLONE_DIR"],
                 "--kernels", kernels_text, "--repeat", ctx.env.get(prefix + "_REPEAT", default_repeat),
                 "--warmup", ctx.env.get(prefix + "_WARMUP", default_warmup), "--output-json", str(output)]
    temporary = Path(ctx.env["TMPDIR"])
    if tool == "ir_serialization":
        arguments += ["--work-root", str(temporary / "benchmark"), "--output-csv", str(ctx.artifacts / "measurements.csv"), "--output-markdown", str(ctx.artifacts / "measurements.md")]
    else:
        arguments += ["--cache-root", str(temporary / "benchmark/cache"), "--dump-root", str(temporary / "benchmark/dump")]
        if tool == "compile_time":
            arguments += ["--output-csv", str(ctx.artifacts / "measurements.csv")]
        else:
            arguments += ["--output-events-csv", str(ctx.artifacts / "events.csv"), "--output-summary-csv", str(ctx.artifacts / "measurements.csv"), "--output-hotspots-markdown", str(ctx.artifacts / "hotspots.md")]
    ctx.run(arguments, cwd=ctx.backend)
    candidate = read_json(output)
    validate_measurements(tool, candidate, kernels)
    candidate.setdefault("metadata", {})["environment_fingerprint"] = ctx.fingerprint
    write_json(output, candidate)
    baseline_path = Path(ctx.env.get("BASELINE_JSON", ""))
    baseline = None
    reason = "baseline_missing"
    if ctx.env.get("BASELINE_JSON") and baseline_path.is_file():
        try:
            baseline = read_json(baseline_path)
            if not ctx.fingerprint or baseline.get("metadata", {}).get("environment_fingerprint") != ctx.fingerprint:
                reason = "environment_fingerprint_mismatch"
                baseline = None
            elif baseline.get("metadata", {}).get("commit_sha") != ctx.env.get("LOCAL_CI_BASE_SHA", ""):
                reason = "base_sha_mismatch"
                baseline = None
            else:
                validate_measurements(tool, baseline, kernels)
        except (ValueError, OSError, ToolError, TypeError) as exc:
            reason = "baseline_invalid: " + str(exc)
            baseline = None
    if baseline is None:
        comparison = {"status": "not_comparable", "reason": reason, "candidate": output.name, "performance_only": True}
        write_json(ctx.artifacts / "comparison.json", comparison)
    else:
        compare_name = {"compile_time": "compare_compile_time.py", "pass_profile": "compare_pass_profile.py", "ir_serialization": "compare_ir_serialization.py"}[tool]
        args = [ctx.python, str(PERFORMANCE / compare_name), "--baseline-json", str(baseline_path), "--candidate-json", str(output),
                "--base-sha", ctx.env.get("LOCAL_CI_BASE_SHA", ""), "--candidate-sha", ctx.sha,
                "--kernels", kernels_text, "--threshold", ctx.env.get(prefix + "_THRESHOLD", "0.20"),
                "--output-json", str(ctx.artifacts / "comparison.json"), "--output-markdown", str(ctx.artifacts / "comparison.md")]
        if tool != "compile_time":
            args += ["--output-csv", str(ctx.artifacts / "comparison.csv")]
        ctx.run(args)
        comparison = read_json(ctx.artifacts / "comparison.json")
    ctx.details["performance"] = comparison


def contract_tests(ctx: Context) -> None:
    output = ctx.artifacts / "contracts.json"
    ctx.run([ctx.python, "-I", str(ROOT / "tools/contract_checks.py"), "--root", str(ctx.anchor),
             "--base", ctx.env.get("LOCAL_CI_BASE_SHA", ""), "--tested", ctx.sha, "--output", str(output)])
    ctx.details["contracts"] = read_json(output)
    paths = [path for path in (ctx.anchor / "scripts/local_ci", ctx.anchor / "scripts/ci/tests", ctx.anchor / "scripts/api_contract/tests")
             if path.is_dir() and any(path.rglob("test_*.py"))]
    ctx.details["candidate_test_directories"] = [str(path.relative_to(ctx.anchor)) for path in paths]
    if paths:
        ctx.run([ctx.python, "-m", "pytest", *map(str, paths), "-q", "--import-mode=importlib"])


HANDLERS = {"environment": environment, "frontend_build": frontend_build,
            "wheel_install_import": wheel_install_import, "frontend_smoke": frontend_smoke,
            "backend_rebuild": backend_rebuild, "backend_smoke_jit": backend_smoke_jit,
            "flaggems": flaggems, "compile_time": performance, "pass_profile": performance,
            "ir_serialization": performance, "contract_tests": contract_tests}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=TOOL_IDS)
    args = parser.parse_args(argv)
    ctx = None
    code = 0
    message = ""
    try:
        ctx = Context(args.tool)
        with (ctx.state / ".tools.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ToolError("Another tool is using this task environment", 2) from exc
            ctx.prepare()
            for successor in (args.tool, *SUCCESSORS.get(args.tool, ())):
                (ctx.state / f"{successor}.json").unlink(missing_ok=True)
            HANDLERS[args.tool](ctx)
            ctx.finish(0)
    except (ToolError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, ToolError) else 2
        message = str(exc)
    except KeyboardInterrupt:
        code, message = 130, "Tool interrupted"
    if code:
        print(message, file=sys.stderr)
        if ctx is not None:
            ctx.finish(code, message)
    return code


def cancelled(signum: int, _frame: Any) -> None:
    raise ToolError(f"Tool cancelled by signal {signum}", 128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancelled)
    raise SystemExit(main())
