"""Git-based Gitee transport; production never requires server-to-GitHub access."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

from .protocol import (
    PREINSTALLED_SUBMODULES,
    RESULT_SCHEMA,
    ContractError,
    atomic_json,
    canonical,
    current_key,
    within,
)
from .delivery import (
    DeliveryPending,
    GiteeReleaseClient,
    MAX_SMALL_JSON,
    delivery_lock,
    publish_evidence,
)


class GitRelay:
    def __init__(
        self,
        url: str,
        root: Path,
        *,
        allow_local: bool = False,
        control_branch: str = "local-ci-control",
        results_branch: str = "local-ci-results",
        attachment_client=None,
    ):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            if parsed.hostname != "gitee.com" or parsed.username or parsed.password:
                raise ContractError(
                    "Production relay must be a credential-free HTTPS Gitee URL"
                )
        elif not allow_local or parsed.scheme not in {"", "file"}:
            raise ContractError(
                "Local relay transports are only allowed in explicit simulations"
            )
        self.url, self.root = url, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.control_branch, self.results_branch = control_branch, results_branch
        self.control_snapshot = None
        self.attachment_client = attachment_client or (
            GiteeReleaseClient(url) if parsed.scheme == "https" else None
        )
        self.cache = self.root / "cache"
        self.lock = threading.RLock()
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if os.getenv("GITEE_TOKEN"):
            askpass = self.root / "askpass.py"
            askpass.write_text(
                "#!/usr/bin/env python3\nimport os,sys\nprint(os.environ.get('GITEE_USERNAME','oauth2') if 'Username' in sys.argv[1] else os.environ['GITEE_TOKEN'])\n"
            )
            askpass.chmod(0o700)
            self.env["GIT_ASKPASS"] = str(askpass)
        if not (self.cache / ".git").exists():
            self.cache.mkdir(exist_ok=True)
            self.git(["init", "-q"], cwd=self.cache)
            self.git(["remote", "add", "origin", url], cwd=self.cache)

    def git(
        self, args: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        directory = (cwd or self.cache).resolve()
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={directory}",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *args,
            ],
            cwd=directory,
            env=self.env,
            capture_output=True,
            timeout=120,
        )
        if check and result.returncode:
            detail = result.stderr.decode(errors="replace")[-2000:]
            for key, value in self.env.items():
                if (
                    any(
                        part in key.upper()
                        for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
                    )
                    and len(value) > 3
                ):
                    detail = detail.replace(value, "[redacted]")
            raise RuntimeError(
                f"Relay git {args[0]} failed with exit {result.returncode}: {detail.strip()}"
            )
        return result

    def refresh(self) -> None:
        with self.lock:
            self.git(
                ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"]
            )
            found = self.git(
                ["rev-parse", "--verify", f"refs/remotes/origin/{self.control_branch}"],
                check=False,
            )
            self.control_snapshot = (
                found.stdout.decode().strip() if found.returncode == 0 else None
            )

    def ref_sha(self, ref: str) -> str:
        return (
            self.git(["rev-parse", "--verify", f"refs/remotes/origin/{ref}^{{commit}}"])
            .stdout.decode()
            .strip()
        )

    def read(self, branch: str, path: str) -> bytes | None:
        within(self.root, path)
        with self.lock:
            revision = (
                self.control_snapshot
                if branch == self.control_branch and self.control_snapshot
                else f"refs/remotes/origin/{branch}"
            )
            result = self.git(["show", f"{revision}:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def read_json(self, branch: str, path: str) -> dict | None:
        raw = self.read(branch, path)
        return json.loads(raw) if raw is not None else None

    def tasks(self) -> list[dict]:
        # Manifest/current/cancel are read from one fetched control commit.
        with self.lock:
            return self._tasks()

    def _tasks(self) -> list[dict]:
        revision = self.control_snapshot or f"refs/remotes/origin/{self.control_branch}"
        result = self.git(
            ["ls-tree", "-r", "--name-only", revision, "current/"], check=False
        )
        if result.returncode:
            return []
        documents = []
        for name in result.stdout.decode().splitlines():
            current = self.read_json(self.control_branch, name)
            if current and current.get("task_id"):
                task = self.read_json(
                    self.control_branch, f"tasks/{current['task_id']}.json"
                )
                if task:
                    documents.append(task)
        return documents

    def validity(self, task: dict) -> tuple[bool, str]:
        cancel = self.read_json(self.control_branch, f"cancel/{task['task_id']}.json")
        if cancel and cancel.get("task_id") == task["task_id"]:
            return False, cancel.get("reason", "Task cancelled")
        current = self.read_json(
            self.control_branch, f"current/{current_key(task)}.json"
        )
        if not current or current.get("task_id") != task["task_id"]:
            return False, "Task superseded or no longer current"
        for field in ("task_ref", "base_task_ref", "head_task_ref"):
            sha_field = {
                "task_ref": "tested_sha",
                "base_task_ref": "base_sha",
                "head_task_ref": "head_sha",
            }[field]
            try:
                actual = self.ref_sha(task[field])
            except RuntimeError:
                return False, f"Task snapshot incomplete: {field}"
            if actual != task[sha_field]:
                return False, f"Task snapshot changed: {field}"
        if task["event_kind"] == "pull_request":
            parents = (
                self.git(["rev-list", "--parents", "-n", "1", task["tested_sha"]])
                .stdout.decode()
                .split()[1:]
            )
            if parents != [task["base_sha"], task["head_sha"]]:
                return False, "Tested merge parents do not match the frozen base/head"
        for variant, sha in (
            ("candidate", task["tested_sha"]),
            ("base", task["base_sha"]),
        ):
            tree = self.git(["ls-tree", "-r", sha]).stdout.decode().splitlines()
            links = {
                row.split("\t", 1)[1]: row.split()[2]
                for row in tree
                if row.startswith("160000 ")
                and row.split("\t", 1)[1] not in PREINSTALLED_SUBMODULES
            }
            modules = {
                row["path"]: row
                for row in task.get("submodules", [])
                if row["variant"] == variant
                and row["path"] not in PREINSTALLED_SUBMODULES
            }
            if set(links) != set(modules):
                return False, "Submodule manifest does not cover the frozen gitlinks"
            for path, module in modules.items():
                if (
                    module["sha"] != links[path]
                    or self.ref_sha(module["task_ref"]) != module["sha"]
                ):
                    return False, "Pinned Gitee submodule snapshot changed"
        llvm = self.git(
            ["show", f"{task['tested_sha']}:triton/cmake/llvm-hash.txt"], check=False
        )
        if llvm.returncode or llvm.stdout.decode().strip() != task["llvm_hash"]:
            return False, "Task LLVM identity does not match tested source"
        return True, "current"

    def checkout(self, sha: str, destination: Path) -> None:
        if destination.exists():
            actual = (
                self.git(["rev-parse", "HEAD"], cwd=destination).stdout.decode().strip()
            )
            dirty = self.git(
                ["status", "--porcelain", "--untracked-files=no"], cwd=destination
            ).stdout
            if actual != sha or dirty:
                raise ContractError(
                    "Existing task checkout does not match the frozen revision"
                )
            return
        self.git(
            [
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                str(self.cache),
                str(destination),
            ],
            cwd=self.root,
        )
        self.git(["checkout", "--quiet", "--detach", sha], cwd=destination)

    def checkout_submodules(self, task: dict, sha: str, destination: Path) -> None:
        """Populate gitlinks from already fetched Gitee refs, ignoring candidate URLs."""
        variant = "candidate" if sha == task["tested_sha"] else "base"
        for module in task.get("submodules", []):
            if (
                module["variant"] != variant
                or module["path"] in PREINSTALLED_SUBMODULES
            ):
                continue
            location = within(destination, module["path"])
            if location.exists() and not any(location.iterdir()):
                location.rmdir()
            self.checkout(module["sha"], location)
            # Nested dependencies also need an explicit mirror manifest.
            nested = self.git(["ls-tree", "-r", module["sha"]]).stdout
            if any(row.startswith(b"160000 ") for row in nested.splitlines()):
                raise ContractError(
                    "Nested submodule requires an explicit mirrored source manifest"
                )

    def write(
        self, branch: str, files: dict[str, bytes], *, immutable: bool = False
    ) -> None:
        for relative in files:
            within(self.root, relative)
        with self.lock:
            last_error = None
            for _ in range(3):
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="publish-", dir=self.root
                    ) as temporary:
                        work = Path(temporary)
                        self.git(["init", "-q"], cwd=work)
                        self.git(["remote", "add", "origin", self.url], cwd=work)
                        found = self.git(
                            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
                            cwd=work,
                        ).stdout.strip()
                        if found:
                            self.git(
                                [
                                    "fetch",
                                    "--quiet",
                                    "--depth=1",
                                    "origin",
                                    f"refs/heads/{branch}",
                                ],
                                cwd=work,
                            )
                            self.git(
                                ["checkout", "--quiet", "-B", branch, "FETCH_HEAD"],
                                cwd=work,
                            )
                        else:
                            self.git(
                                ["checkout", "--quiet", "--orphan", branch], cwd=work
                            )
                        # Retention and uploads share this fetched Git snapshot. A
                        # competing push retries the whole decision against new HEAD.
                        pending = (
                            self._unexpired_files(work, files)
                            if immutable and branch == self.results_branch
                            else files
                        )
                        if not pending:
                            return
                        for relative, content in pending.items():
                            path = within(work, relative)
                            if (
                                immutable
                                and path.exists()
                                and path.read_bytes() != content
                            ):
                                raise ContractError(
                                    f"Immutable relay artifact changed: {relative}"
                                )
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(content)
                        self.git(["add", "--", *pending.keys()], cwd=work)
                        if (
                            self.git(
                                ["diff", "--cached", "--quiet"], cwd=work, check=False
                            ).returncode
                            == 0
                        ):
                            return
                        self.git(
                            [
                                "-c",
                                "user.name=local-ci",
                                "-c",
                                "user.email=local-ci@example.invalid",
                                "commit",
                                "--quiet",
                                "-m",
                                "ci: publish durable task evidence",
                            ],
                            cwd=work,
                        )
                        self.git(
                            ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"],
                            cwd=work,
                        )
                        return
                except RuntimeError as exc:
                    last_error = exc
            raise RuntimeError(
                "Relay publish failed after three attempts"
            ) from last_error

    @staticmethod
    def _unexpired_files(work: Path, files: dict[str, bytes]) -> dict[str, bytes]:
        """A matching expiry marker proves this sealed result was uploaded earlier."""
        work = work.resolve()
        runs: dict[tuple[str, str], list[str]] = {}
        for relative in files:
            normalized = within(work, relative).relative_to(work).as_posix()
            match = re.fullmatch(
                r"runs/v4/([0-9a-f]{64})/([A-Za-z0-9][A-Za-z0-9_.-]{0,119})/(.+)",
                normalized,
            )
            if match:
                runs.setdefault((match[1], match[2]), []).append(relative)
        skipped = set()
        for (task_id, run_id), relatives in runs.items():
            marker = work / "retention/v4" / task_id / (run_id + ".json")
            if marker.is_symlink() or any(
                p.is_symlink()
                for p in marker.parents
                if p != work and p.is_relative_to(work)
            ):
                raise ContractError("Retention marker path contains a symlink")
            if not marker.exists():
                continue
            prefix = f"runs/v4/{task_id}/{run_id}"
            if not marker.is_file() or (work / prefix).exists():
                raise ContractError(
                    "Invalid retention marker or expired result tree exists"
                )
            raw = files.get(prefix + "/result.json")
            try:
                saved = json.loads(marker.read_bytes())
                result = json.loads(raw) if raw is not None else None
                valid = (
                    isinstance(saved, dict)
                    and saved.get("schema") == "triton-anchor-result-retention/v1"
                    and saved.get("task_id") == task_id
                    and saved.get("run_id") == run_id
                    and saved.get("reason") == "retention_expired"
                    and raw is not None
                    and saved.get("result_digest") == hashlib.sha256(raw).hexdigest()
                    and isinstance(result, dict)
                    and result.get("schema") == RESULT_SCHEMA
                    and isinstance(result.get("task"), dict)
                    and result["task"].get("task_id") == task_id
                    and result.get("run_id") == run_id
                )
            except (ValueError, TypeError, UnicodeError):
                valid = False
            if not valid:
                raise ContractError(
                    "Expired result replay does not match its immutable retention marker"
                )
            skipped.update(relatives)
        return {
            relative: content
            for relative, content in files.items()
            if relative not in skipped
        }

    def publish_result(
        self, task: dict, run_id: str, directory: Path, *, optional_only: bool = False
    ) -> str:
        with delivery_lock(directory.parent):
            return self._publish_result(
                task, run_id, directory, optional_only=optional_only
            )

    def _publish_result(
        self, task: dict, run_id: str, directory: Path, *, optional_only: bool = False
    ) -> str:
        prefix = f"runs/v4/{task['task_id']}/{run_id}"
        files = {}
        for name in ("result.json", "execution-summary.json"):
            source = directory / name
            if source.is_symlink():
                raise ContractError("Symlinks cannot be published as evidence")
            if not source.is_file() or source.stat().st_size > MAX_SMALL_JSON:
                raise ContractError(
                    f"Missing or oversized sealed control document: {name}"
                )
            files[f"{prefix}/{name}"] = source.read_bytes()
        result_raw = files[f"{prefix}/result.json"]
        result = json.loads(result_raw)
        if result.get("task") != task or result.get("run_id") != run_id:
            raise ContractError(
                "Sealed result task/run differs from publication request"
            )
        if (
            result.get("execution_summary_sha256")
            != hashlib.sha256(files[f"{prefix}/execution-summary.json"]).hexdigest()
        ):
            raise ContractError("Execution summary differs from sealed result")
        result_digest = hashlib.sha256(result_raw).hexdigest()
        # Git never receives logs, reports, wheels or an archive of the run.
        self.write(self.results_branch, files, immutable=True)
        local_index = directory.parent / "delivery-index.json"
        saved = json.loads(local_index.read_bytes()) if local_index.exists() else None
        index = publish_evidence(
            self.attachment_client,
            task,
            run_id,
            directory,
            result,
            result_digest,
            saved,
            optional_only=optional_only,
        )
        atomic_json(local_index, index)
        self.write(
            self.results_branch,
            {f"{prefix}/delivery-index.json": canonical(index) + b"\n"},
        )
        if index["status"] != "ready" and not optional_only:
            raise DeliveryPending(
                "Required Gitee evidence remains pending; sealed test outcome is unchanged"
            )
        return result_digest
