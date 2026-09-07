"""Git-based Gitee transport; production never requires server-to-GitHub access."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

from .protocol import ContractError, current_key, within


class GitRelay:
    def __init__(self, url: str, root: Path, *, allow_local: bool = False,
                 control_branch: str = "local-ci-control", results_branch: str = "local-ci-results"):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            if parsed.hostname != "gitee.com" or parsed.username or parsed.password:
                raise ContractError("Production relay must be a credential-free HTTPS Gitee URL")
        elif not allow_local or parsed.scheme not in {"", "file"}:
            raise ContractError("Local relay transports are only allowed in explicit simulations")
        self.url, self.root = url, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_branch, self.results_branch = control_branch, results_branch
        self.cache = self.root / "cache"
        self.lock = threading.RLock()
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        if os.getenv("GITEE_TOKEN"):
            askpass = self.root / "askpass.py"
            askpass.write_text("#!/usr/bin/env python3\nimport os,sys\nprint(os.environ.get('GITEE_USERNAME','oauth2') if 'Username' in sys.argv[1] else os.environ['GITEE_TOKEN'])\n")
            askpass.chmod(0o700)
            self.env["GIT_ASKPASS"] = str(askpass)
        if not (self.cache / ".git").exists():
            self.cache.mkdir(exist_ok=True)
            self.git(["init", "-q"], cwd=self.cache)
            self.git(["remote", "add", "origin", url], cwd=self.cache)

    def git(self, args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        directory = (cwd or self.cache).resolve()
        result = subprocess.run(["git", "-c", f"safe.directory={directory}", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
                                cwd=directory, env=self.env, capture_output=True, timeout=120)
        if check and result.returncode:
            detail = result.stderr.decode(errors="replace")[-2000:]
            for key, value in self.env.items():
                if any(part in key.upper() for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")) and len(value) > 3:
                    detail = detail.replace(value, "[redacted]")
            raise RuntimeError(f"Relay git {args[0]} failed with exit {result.returncode}: {detail.strip()}")
        return result

    def refresh(self) -> None:
        with self.lock:
            self.git(["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"])

    def ref_sha(self, ref: str) -> str:
        return self.git(["rev-parse", "--verify", f"refs/remotes/origin/{ref}^{{commit}}"]).stdout.decode().strip()

    def read(self, branch: str, path: str) -> bytes | None:
        within(self.root, path)
        with self.lock:
            result = self.git(["show", f"refs/remotes/origin/{branch}:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def read_json(self, branch: str, path: str) -> dict | None:
        raw = self.read(branch, path)
        return json.loads(raw) if raw is not None else None

    def tasks(self) -> list[dict]:
        result = self.git(["ls-tree", "-r", "--name-only", f"refs/remotes/origin/{self.control_branch}", "current/"], check=False)
        if result.returncode:
            return []
        documents = []
        for name in result.stdout.decode().splitlines():
            current = self.read_json(self.control_branch, name)
            if current and current.get("task_id"):
                task = self.read_json(self.control_branch, f"tasks/{current['task_id']}.json")
                if task:
                    documents.append(task)
        return documents

    def validity(self, task: dict) -> tuple[bool, str]:
        cancel = self.read_json(self.control_branch, f"cancel/{task['task_id']}.json")
        if cancel and cancel.get("task_id") == task["task_id"]:
            return False, cancel.get("reason", "Task cancelled")
        current = self.read_json(self.control_branch, f"current/{current_key(task)}.json")
        if not current or current.get("task_id") != task["task_id"]:
            return False, "Task superseded or no longer current"
        for field in ("task_ref", "base_task_ref", "head_task_ref"):
            sha_field = {"task_ref": "tested_sha", "base_task_ref": "base_sha", "head_task_ref": "head_sha"}[field]
            try:
                actual = self.ref_sha(task[field])
            except RuntimeError:
                return False, f"Task snapshot incomplete: {field}"
            if actual != task[sha_field]:
                return False, f"Task snapshot changed: {field}"
        if task["event_kind"] == "pull_request":
            parents = self.git(["rev-list", "--parents", "-n", "1", task["tested_sha"]]).stdout.decode().split()[1:]
            if parents != [task["base_sha"], task["head_sha"]]:
                return False, "Tested merge parents do not match the frozen base/head"
        llvm = self.git(["show", f"{task['tested_sha']}:triton/cmake/llvm-hash.txt"], check=False)
        if llvm.returncode or llvm.stdout.decode().strip() != task["llvm_hash"]:
            return False, "Task LLVM identity does not match tested source"
        return True, "current"

    def checkout(self, sha: str, destination: Path) -> None:
        if destination.exists():
            actual = self.git(["rev-parse", "HEAD"], cwd=destination).stdout.decode().strip()
            dirty = self.git(["status", "--porcelain", "--untracked-files=no"], cwd=destination).stdout
            if actual != sha or dirty:
                raise ContractError("Existing task checkout does not match the frozen revision")
            return
        self.git(["clone", "--quiet", "--no-hardlinks", "--no-checkout", str(self.cache), str(destination)], cwd=self.root)
        self.git(["checkout", "--quiet", "--detach", sha], cwd=destination)

    def write(self, branch: str, files: dict[str, bytes], *, immutable: bool = False) -> None:
        for relative in files:
            within(self.root, relative)
        with self.lock:
            last_error = None
            for _ in range(3):
                try:
                    with tempfile.TemporaryDirectory(prefix="publish-", dir=self.root) as temporary:
                        work = Path(temporary)
                        self.git(["init", "-q"], cwd=work)
                        self.git(["remote", "add", "origin", self.url], cwd=work)
                        found = self.git(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=work).stdout.strip()
                        if found:
                            self.git(["fetch", "--quiet", "--depth=1", "origin", f"refs/heads/{branch}"], cwd=work)
                            self.git(["checkout", "--quiet", "-B", branch, "FETCH_HEAD"], cwd=work)
                        else:
                            self.git(["checkout", "--quiet", "--orphan", branch], cwd=work)
                        for relative, content in files.items():
                            path = within(work, relative)
                            if immutable and path.exists() and path.read_bytes() != content:
                                raise ContractError(f"Immutable relay artifact changed: {relative}")
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(content)
                        self.git(["add", "--", *files.keys()], cwd=work)
                        if self.git(["diff", "--cached", "--quiet"], cwd=work, check=False).returncode == 0:
                            return
                        self.git(["-c", "user.name=local-ci", "-c", "user.email=local-ci@example.invalid",
                                  "commit", "--quiet", "-m", "ci: publish durable task evidence"], cwd=work)
                        self.git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=work)
                        return
                except RuntimeError as exc:
                    last_error = exc
            raise RuntimeError("Relay publish failed after three attempts") from last_error

    def publish_result(self, task: dict, run_id: str, directory: Path) -> str:
        prefix = f"runs/v4/{task['task_id']}/{run_id}"
        files = {}
        for source in directory.rglob("*"):
            if source.is_symlink():
                raise ContractError("Symlinks cannot be published as evidence")
            if source.is_file():
                if source.stat().st_size > 64 * 1024 * 1024:
                    raise ContractError("Individual evidence file exceeds 64 MiB")
                files[f"{prefix}/{source.relative_to(directory).as_posix()}"] = source.read_bytes()
        self.write(self.results_branch, files, immutable=True)
        return hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()

    def receipt(self, task: dict, run_id: str) -> dict | None:
        return self.read_json(self.control_branch, f"receipts/{task['task_id']}/{run_id}.json")
