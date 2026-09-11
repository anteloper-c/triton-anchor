#!/usr/bin/env python3
"""Run the unified A tool plan to validate a trusted image candidate."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def validation_context(workspace, environment):
    source = Path(environment.get("ANCHOR_DIR", str(workspace / "triton-anchor")))
    seed = environment.get("SEED_PYTHON") or str(
        Path(environment.get("PYTHON_VENV_ACTIVATE", "/opt/venv/bin/activate")).parent
        / "python"
    )
    target = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tools = {
        "llvm_dir": environment.get("LLVM_BUILD_DIR"),
        "backend_dir": environment.get("BACKEND_PATH"),
        "backend_wheel_pattern": environment.get("BACKEND_WHEEL_PATTERN"),
        "expected_backend": environment.get("EXPECTED_TRITON_BACKEND"),
        "flaggems_dir": environment.get("FLAGGEMS_CLONE_DIR"),
        "backend_test_paths": shlex.split(
            environment.get("BACKEND_TEST_PATHS", "tests")
        ),
        "backend_smoke_argv": ["bash", "-c", environment["BACKEND_TEST_COMMAND"]]
        if environment.get("BACKEND_TEST_COMMAND")
        else [],
        "env": dict(environment),
    }
    if environment.get("TRUSTED_ANCHOR_ENVSETUP"):
        tools["env_scripts"] = [
            {"path": environment["TRUSTED_ANCHOR_ENVSETUP"], "args": []}
        ]
    if environment.get("BACKEND_ENVSETUP"):
        setup = Path(environment["BACKEND_ENVSETUP"])
        if not setup.is_absolute():
            setup = Path(environment["BACKEND_PATH"]) / setup
        tools["backend_env_scripts"] = [
            {
                "path": str(setup),
                "args": shlex.split(environment.get("BACKEND_ENVSETUP_ARGS", "")),
            }
        ]
    return {
        "source_dir": str(source),
        "artifact_dir": str(workspace / "environment-validation/artifacts"),
        "task_id": "image-validation",
        "target_sha": target,
        "triton_version": environment.get("LOCAL_CI_TRITON_VERSION", ""),
        "python_bin": seed,
        "trusted_python_bin": sys.executable,
        "tools_dir": str(Path(__file__).resolve().parents[1] / "tools"),
        "task_root": str(workspace / "environment-validation"),
        "profile": {
            "id": "image-validation",
            "llvm_revision": environment.get("LOCAL_CI_LLVM_HASH"),
            "backend_enabled": environment.get("RUN_BACKEND_STAGES") == "true",
            "tools": tools,
        },
    }


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.basic_tools.runner import TOOL_IDS

    tool = sys.argv[1] if len(sys.argv) == 2 else ""
    if tool not in TOOL_IDS:
        print("Supply a unified Local CI tool ID", file=sys.stderr)
        return 2
    workspace = Path(os.environ.get("WORKSPACE", "/opt/local-ci/runtime"))
    if not workspace.is_absolute() or workspace == Path("/"):
        raise ValueError("Invalid image-validation workspace")
    context = validation_context(workspace, dict(os.environ))
    path = workspace / "environment-validation/context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(context))
    runner = Path(__file__).resolve().parents[1] / "tools/basic_tools/runner.py"
    return subprocess.run(
        [
            sys.executable,
            str(runner),
            tool,
            "--context",
            str(path),
            "--parameters",
            "{}",
            "--execute",
        ]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
