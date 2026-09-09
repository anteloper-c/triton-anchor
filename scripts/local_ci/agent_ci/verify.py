#!/usr/bin/env python3
"""Offline behavioral acceptance runner. Never invokes a real model or remote CI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUITES = {
    "worker-mcp-recovery": ["scripts/local_ci/agent_ci/tests"],
    "real-tool-interfaces": ["scripts/local_ci/tools/tests"],
    "images-task-containers": ["scripts/local_ci/environments/tests"],
    "deployment-monitoring": ["scripts/local_ci/deploy/tests", "scripts/local_ci/maintenance/tests"],
    "github-gateway": ["scripts/ci/tests"],
    "retained-local-contracts": ["scripts/local_ci/tests", "scripts/local_ci/results/tests", "scripts/local_ci/codex_ai/tests"],
    "api-contracts": ["scripts/api_contract/tests"],
    "frontend-performance-dashboard": ["python/triton_anchor/tests"],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "triton-anchor-ci-v4-verification/v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "mode": "offline-simulation", "real_model_calls": False, "real_backend_builds": False, "real_docker_daemon": False,
              "remote_writes": False, "actual_emails": False, "suites": []}
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0"}
    environment["PYTHONPATH"] = str(ROOT / "python") + os.pathsep + environment.get("PYTHONPATH", "")
    # No production credentials are needed by the simulation fixtures.
    for key in list(environment):
        if any(part in key.upper() for part in ("TOKEN", "API_KEY", "PASSWORD", "SECRET")):
            environment.pop(key)
    for name, paths in SUITES.items():
        present = [p for p in paths if (ROOT / p).exists()]
        if len(present) != len(paths):
            report["suites"].append({"name": name, "status": "missing", "paths": paths, "exit_code": 2})
            continue
        command = [sys.executable, "-m", "pytest", "-q", "--import-mode=importlib", *present]
        completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (output / (name + ".log")).write_text(completed.stdout)
        count = re.search(r"(\d+) passed", completed.stdout)
        report["suites"].append({"name": name, "command": command, "exit_code": completed.returncode,
                                 "status": "pass" if completed.returncode == 0 else "fail", "passed": int(count[1]) if count else 0,
                                 "log": name + ".log"})
        print(name + ": " + report["suites"][-1]["status"], flush=True)
    syntax_errors = []
    for directory in (ROOT / "scripts/local_ci", ROOT / "scripts/ci"):
        for path in directory.rglob("*.py"):
            try:
                compile(path.read_text(), str(path), "exec")
            except (SyntaxError, UnicodeError) as exc:
                syntax_errors.append(str(exc))
        for path in directory.rglob("*.sh"):
            result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            if result.returncode:
                syntax_errors.append(result.stderr)
    report["syntax_errors"] = syntax_errors
    report["passed"] = sum(row.get("passed", 0) for row in report["suites"])
    report["success"] = not syntax_errors and all(row["exit_code"] == 0 for row in report["suites"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["source_digest"] = hashlib.sha256(b"".join(
        str(p.relative_to(ROOT)).encode() + b"\0" + p.read_bytes()
        for directory in (ROOT / "scripts/local_ci", ROOT / "scripts/ci", ROOT / ".github")
        for p in sorted(directory.rglob("*")) if p.is_file() and (p.suffix in {".py", ".sh", ".json", ".yml", ".yaml", ".md", ".service", ".timer", ".toml"} or p.name == "Dockerfile")
    )).hexdigest()
    router = ROOT.parent / "triton-anchor-main/.github/workflows/ci-gateway.yml"
    report["main_router_digest"] = hashlib.sha256(router.read_bytes()).hexdigest() if router.is_file() else None
    (output / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    lines = ["# Local CI v4 本机模拟验收", "", f"结果：{'通过' if report['success'] else '未通过'}；通过测试 {report['passed']} 项。", "",
             "| 测试集 | 结果 | 通过数 | 日志 |", "| --- | --- | ---: | --- |"]
    lines += [f"| {row['name']} | {row['status']} | {row.get('passed', 0)} | {row.get('log', '')} |" for row in report["suites"]]
    lines += ["", "验证真实控制逻辑与工具接口，外部边界使用 fixtures。未使用真实 Docker daemon 或容器命名空间；未验证真实模型、LLVM/后端构建、硬件、实际邮件或线上 GitHub/Gitee 回写。", "", f"Source digest: {report['source_digest']}"]
    (output / "verification.md").write_text("\n".join(lines) + "\n")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
