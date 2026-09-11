"""Build-time dependency preparation only; no task controller or deployment config."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


def prepare_image():
    recipe = json.loads(Path("/opt/local-ci/image-recipe.json").read_text())
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        **recipe["env"],
    }
    llvm = recipe["llvm"]
    if llvm["mode"] == "source":
        source = "/opt/local-ci/runtime/deps/llvm-source/llvm"
        build = "/opt/local-ci/runtime/deps/llvm-build"
        arguments = llvm.get(
            "cmake_args",
            [
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLVM_ENABLE_PROJECTS=mlir;clang;lld",
                "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
            ],
        )
        subprocess.run(
            [
                "cmake",
                "-S",
                source,
                "-B",
                build,
                *arguments,
                "-DCMAKE_INSTALL_PREFIX=" + environment["LLVM_BUILD_DIR"],
            ],
            check=True,
            env=environment,
        )
        subprocess.run(
            [
                "cmake",
                "--build",
                build,
                "--target",
                "install",
                "--parallel",
                str(recipe.get("build_jobs", 8)),
            ],
            check=True,
            env=environment,
        )
        shutil.rmtree(build)
        shutil.rmtree("/opt/local-ci/runtime/deps/llvm-source")
    for command in recipe.get("prepare_commands", []):
        subprocess.run(
            command, check=True, env=environment, cwd="/opt/local-ci/runtime"
        )
    root = Path("/opt/local-ci/runtime")
    # COPY can inherit a private host umask; the non-root task user needs read access.
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("Unexpected special file in dependency image")
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() or mode & 0o111 else 0o644)


if __name__ == "__main__":
    prepare_image()
