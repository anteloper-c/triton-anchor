#!/usr/bin/env python3
"""Call a real basic tool while validating a trusted image candidate."""
import os
import subprocess
import sys
from pathlib import Path


def main():
    tool = sys.argv[1] if len(sys.argv) == 2 else ""
    allowed = {"environment", "frontend_build", "wheel_install_import", "frontend_smoke", "backend_rebuild", "backend_smoke_jit", "flaggems", "compile_time", "pass_profile", "ir_serialization"}
    if tool not in allowed:
        print("Supply a real Local CI validation tool ID", file=sys.stderr)
        return 2
    workspace = Path(os.environ.get("WORKSPACE", "/workspace"))
    if not workspace.is_absolute() or workspace == Path("/"):
        raise ValueError("Invalid image-validation workspace")
    env = dict(os.environ)
    env.setdefault("ANCHOR_DIR", str(workspace / "triton-anchor"))
    env["LOCAL_CI_TASK_ROOT"] = str(workspace / "environment-validation")
    env["LOCAL_CI_ARTIFACT_DIR"] = str(workspace / "environment-validation" / "artifacts" / tool)
    env["LOCAL_CI_TOOL_RESULT"] = str(Path(env["LOCAL_CI_ARTIFACT_DIR"]) / "result.json")
    runner = Path(__file__).resolve().parents[1] / "tools/run_tool.py"
    return subprocess.run([sys.executable, str(runner), tool], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
