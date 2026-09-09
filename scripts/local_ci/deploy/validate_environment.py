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
    result = subprocess.run([sys.executable, str(runner), tool], env=env)
    if result.returncode:
        # Preserve bounded diagnostics before the temporary validation container is removed.
        root = Path(env["LOCAL_CI_ARTIFACT_DIR"])
        for path in sorted(root.glob("*.log")):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 16384))
                tail = handle.read(16384).decode("utf-8", errors="replace")
            print(f"\n[{tool}] {path.name}\n{tail}", file=sys.stderr, flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
