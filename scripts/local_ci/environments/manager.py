#!/usr/bin/env python3
"""Prepare, lease and rotate persistent environments from trusted recipes.

No recipe is read from a PR checkout. Docker containers belong to generations,
not individual tasks. State and receipts remain on the host when generations
are rotated. This module does not invoke a model or contact GitHub.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA = "triton-anchor-local-ci-environments/v1"
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")


class EnvironmentError(RuntimeError):
    """A requested environment could not be prepared or safely selected."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    """Fingerprint an immutable dependency tree, including internal link targets."""
    entries = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise EnvironmentError("Prepared dependency contains a link outside its install tree")
            entries.append([relative, "link", os.readlink(path)])
        elif path.is_file():
            entries.append([relative, "file", file_digest(path), path.stat().st_mode & 0o777])
        elif path.is_dir():
            entries.append([relative, "directory"])
        else:
            raise EnvironmentError("Prepared dependency contains a special file")
    return fingerprint(entries)


def absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise EnvironmentError(f"{label} must be an absolute server path")
    return Path(value)


def safe_source(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvironmentError(f"{label} is required")
    parsed = urllib.parse.urlparse(value)
    if parsed.username or parsed.password:
        raise EnvironmentError(f"{label} must not contain embedded credentials")
    if parsed.scheme:
        if parsed.scheme not in {"https", "file"}:
            raise EnvironmentError(f"{label} must use HTTPS or a local file")
        if (parsed.hostname or "").lower() in {"github.com", "api.github.com", "raw.githubusercontent.com"}:
            raise EnvironmentError(f"{label} must use a server-reachable mirror; GitHub is not a Local CI source")
    elif not Path(value).is_absolute():
        raise EnvironmentError(f"{label} must be an absolute local path or HTTPS mirror")
    return value


def extract_verified_archive(archive: Path, destination: Path, expected_sha256: str, strip_components: int = 1) -> None:
    """Extract files plus internal links, never following a link while writing."""
    if not DIGEST_RE.fullmatch(expected_sha256) or file_digest(archive) != expected_sha256:
        raise EnvironmentError("Dependency archive SHA256 does not match trusted recipe")
    if strip_components < 0:
        raise EnvironmentError("strip_components must not be negative")
    destination.mkdir(parents=True, exist_ok=False)
    links: list[tuple[Path, str, bool]] = []

    def target(name: str) -> Path | None:
        parts = name.replace("\\", "/").split("/")
        if name.startswith(("/", "\\")) or any(part == ".." for part in parts) or ":" in parts[0]:
            raise EnvironmentError("Dependency archive contains an unsafe path")
        parts = [part for part in parts if part not in {"", "."}][strip_components:]
        return destination.joinpath(*parts) if parts else None

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as handle:
                for member in handle.infolist():
                    mode = member.external_attr >> 16
                    output = target(member.filename)
                    if output is None:
                        continue
                    if (mode & 0o170000) == 0o120000:
                        links.append((output, handle.read(member).decode("utf-8"), False))
                        continue
                    if member.is_dir():
                        output.mkdir(parents=True, exist_ok=True)
                    else:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with handle.open(member) as source, output.open("wb") as sink:
                            shutil.copyfileobj(source, sink)
                        if mode:
                            output.chmod(mode & 0o777)
        else:
            with tarfile.open(archive) as handle:
                for member in handle:
                    if not any((member.isfile(), member.isdir(), member.issym(), member.islnk())):
                        raise EnvironmentError("Dependency archives must not contain special device files")
                    output = target(member.name)
                    if output is None:
                        continue
                    if member.issym() or member.islnk():
                        linked = target(member.linkname) if member.islnk() else None
                        if member.islnk() and linked is None:
                            raise EnvironmentError("Invalid archive hard-link target")
                        links.append((output, str(linked) if linked else member.linkname, member.islnk()))
                        continue
                    if member.isdir():
                        output.mkdir(parents=True, exist_ok=True)
                    else:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        source = handle.extractfile(member)
                        if source is None:
                            raise EnvironmentError("Unable to read dependency archive member")
                        with source, output.open("wb") as sink:
                            shutil.copyfileobj(source, sink)
                        output.chmod(member.mode & 0o777)
        # Deferring links prevents an earlier archive entry redirecting file
        # extraction. Real LLVM releases contain internal shared-library links.
        for output, link, hard in links:
            if output.exists() or output.is_symlink():
                raise EnvironmentError("Archive link collides with an extracted file")
            if not hard and (link.startswith(("/", "\\")) or ":" in link.split("/")[0]):
                raise EnvironmentError("Archive symlink has an absolute target")
            linked = Path(link) if hard else output.parent / link
            if not linked.resolve().is_relative_to(destination.resolve()):
                raise EnvironmentError("Archive link escapes the dependency directory")
            output.parent.mkdir(parents=True, exist_ok=True)
            if hard:
                if not linked.is_file():
                    raise EnvironmentError("Archive hard-link target is not a regular file")
                os.link(linked, output)
            else:
                output.symlink_to(link)
    except BaseException:
        shutil.rmtree(destination)
        raise


class EnvironmentManager:
    def __init__(self, config: dict[str, Any], state_dir: str | Path, runner: Callable[..., Any] | None = None):
        self.config = config
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.directory = self.state_dir / "environments"
        self.directory.mkdir(exist_ok=True)
        self.registry = self.directory / "registry.json"
        self.runner = runner or subprocess.run
        self.cancel_event = None
        self.docker = str(config.get("docker_bin", "docker"))

    @contextlib.contextmanager
    def _locked(self):
        # The supervisor must not acquire this same flock before manager calls.
        with (self.state_dir / "resource.lock").open("a+") as handle:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise EnvironmentError("Environment lease cancelled while waiting for resources")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.25)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.registry.exists():
            return {"schema": SCHEMA, "active": {}, "generations": {}, "leases": {}, "events": []}
        try:
            document = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EnvironmentError("Environment registry is unreadable; refusing to recreate it") from exc
        if document.get("schema") != SCHEMA:
            raise EnvironmentError("Unsupported environment registry schema")
        return document

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_json(self.registry, state)

    def _event(self, state: dict[str, Any], kind: str, **values: Any) -> None:
        event = {"at": utc_now(), "event": kind, **values}
        state["events"] = [*state.get("events", []), event][-200:]
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _run(self, argv: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> str:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise EnvironmentError("Environment preparation cancelled because the task is no longer current")
        try:
            limit = timeout or int(self.config.get("environment_command_timeout", 14400))
            if self.runner is subprocess.run:
                # communicate(timeout) drains both pipes and makes cancellation
                # responsive even during LLVM source builds and git transfers.
                process = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                started = time.monotonic()
                while True:
                    cancelled = self.cancel_event is not None and self.cancel_event.is_set()
                    if cancelled or time.monotonic() - started >= limit:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.communicate()
                        raise EnvironmentError("Environment preparation cancelled" if cancelled else "Environment command timed out")
                    try:
                        stdout, stderr = process.communicate(timeout=0.5)
                        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            else:
                result = self.runner(argv, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=limit)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentError(f"Environment command could not finish: {argv[0]}") from exc
        if result.returncode:
            # Full command output can contain private URLs. Keep it local only.
            with (self.directory / "preparation.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} executable={argv[0]} exit={result.returncode}\n")
                handle.write((result.stdout or "") + (result.stderr or "") + "\n")
            raise EnvironmentError(f"Environment command failed: {argv[0]} (exit {result.returncode}); see local preparation.log")
        return result.stdout or ""

    def _profile(self, target_branch: str, llvm_hash: str) -> tuple[str, dict[str, Any]]:
        if not SHA_RE.fullmatch(llvm_hash):
            raise EnvironmentError("LLVM revision must be an exact lowercase commit SHA")
        profiles = self.config.get("profiles", {})
        if target_branch not in profiles or not isinstance(profiles[target_branch], dict):
            raise EnvironmentError(f"No trusted environment recipe for target branch {target_branch}")
        recipe = copy.deepcopy(profiles[target_branch])
        profile = recipe.get("name", target_branch.replace("/", "-"))
        if not isinstance(profile, str) or not NAME_RE.fullmatch(profile):
            raise EnvironmentError("Environment profile name is invalid")
        if not SHA_RE.fullmatch(str(recipe.get("llvm_hash", ""))):
            raise EnvironmentError("Trusted profile must declare its current exact LLVM revision")
        if not isinstance(recipe.get("backend_enabled", False), bool):
            raise EnvironmentError("backend_enabled must be a trusted boolean capability")
        if recipe.get("backend_enabled") and str(recipe.get("triton_version", "")).split(".")[:2] != ["3", "0"]:
            raise EnvironmentError("Only the deployed Triton 3.0 profile may enable backend stages")
        llvm = recipe.get("llvm", {})
        if not isinstance(llvm, dict):
            raise EnvironmentError("llvm recipe must be an object")
        revisions = llvm.pop("revisions", {})
        if llvm_hash in revisions:
            llvm.update(revisions[llvm_hash])
        elif llvm_hash != recipe["llvm_hash"] and llvm.get("mode") != "source":
            raise EnvironmentError("New LLVM revision needs a trusted archive recipe or source mirror; backend capability is unchanged")
        recipe["llvm"] = llvm
        recipe["requested_llvm_hash"] = llvm_hash
        control_root = absolute_path(self.config.get("control_root"), "control_root")
        revision = self._run(["git", "-C", str(control_root), "rev-parse", "HEAD"]).strip()
        if not SHA_RE.fullmatch(revision):
            raise EnvironmentError("Trusted control checkout has no exact Git revision")
        dirty = self._run(["git", "-C", str(control_root), "-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=normal", "--",
                           "scripts/local_ci", "scripts/ci", "scripts/api_contract", "api_contract", ".github"])
        if dirty.strip() and self.config.get("simulation") is not True:
            raise EnvironmentError("Environment preparation requires a clean installed trusted control revision")
        recipe["control_revision_sha"] = revision
        return profile, recipe

    def _inspect(self, container: str) -> dict[str, Any]:
        try:
            result = json.loads(self._run([self.docker, "inspect", container], timeout=30))
            if not isinstance(result, list) or len(result) != 1:
                raise ValueError("expected one container")
            return result[0]
        except (ValueError, IndexError) as exc:
            raise EnvironmentError("Docker returned invalid container inspection") from exc

    def _verify_container(self, generation: dict[str, Any]) -> None:
        info = self._inspect(generation["container"])
        if not info.get("State", {}).get("Running"):
            raise EnvironmentError(f"Persistent container {generation['container']} is unavailable")
        if generation.get("container_id") and info.get("Id") != generation["container_id"]:
            raise EnvironmentError("Persistent container identity changed outside environment manager")
        if not generation.get("external"):
            labels = info.get("Config", {}).get("Labels", {}) or {}
            if labels.get("triton-anchor.generation") != generation["generation"]:
                raise EnvironmentError("Persistent container ownership does not match registry")

    def _execution_identity(self, generation: dict[str, Any], recipe: dict[str, Any]) -> None:
        user = recipe.get("execution_user", self.config.get("container_execution_user", ""))
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", user):
            raise EnvironmentError("Configure the actual non-root execution_user in the persistent image")
        values = []
        for flag in ("-u", "-g"):
            value = self._run([self.docker, "exec", "--user", user, generation["container"], "id", flag]).strip()
            if not value.isdigit() or int(value) == 0:
                raise EnvironmentError("Container execution user and group must both be non-root numeric identities")
            values.append(int(value))
        if self.config.get("codex_user"):
            try:
                codex_uid = pwd.getpwnam(self.config["codex_user"]).pw_uid
            except KeyError as exc:
                raise EnvironmentError("Dedicated host Codex account is not provisioned") from exc
            if codex_uid == values[0]:
                raise EnvironmentError("Container task UID must differ from host Codex UID to protect persistent session credentials")
        generation.update(execution_user=user, execution_uid=values[0], execution_gid=values[1])

    def _runtime_access(self, generation: dict[str, Any], *, managed: bool) -> None:
        workspace = Path(generation["workspace_host"])
        if managed:
            # Journals/secrets use UMask=0077. The container bind root and
            # dependency trees need explicit traversal for the distinct task UID.
            for path in (workspace, *workspace.rglob("*")):
                if path.is_symlink():
                    continue
                mode = path.stat().st_mode & 0o777
                if path.is_dir():
                    path.chmod((mode | 0o055) & ~0o022)
                elif path.is_file():
                    path.chmod((mode | 0o044 | (0o011 if mode & 0o100 else 0)) & ~0o022)
        prefix = [self.docker, "exec", "--user", generation["execution_user"], generation["container"]]
        self._run([*prefix, "test", "-x", generation["workspace_container"]])
        self._run([*prefix, "test", "-r", "/opt/local-ci/control/scripts/local_ci/tools/run_tool.py"])
        self._run([*prefix, generation["env"]["LLVM_BUILD_DIR"] + "/bin/llvm-config", "--version"])
        seed = generation["env"].get("SEED_PYTHON")
        if not seed and generation["env"].get("PYTHON_VENV_ACTIVATE"):
            seed = str(Path(generation["env"]["PYTHON_VENV_ACTIVATE"]).parent / "python")
        if not seed or not Path(seed).is_absolute():
            raise EnvironmentError("Profile must identify its actual absolute seed Python or PYTHON_VENV_ACTIVATE")
        self._run([*prefix, seed, "--version"])
        self._run([*prefix, seed, "-c", "import build, setuptools, wheel, pybind11, yaml, pytest"])

    def _download(self, source: str, digest: str) -> Path:
        safe_source(source, "Dependency source")
        if not DIGEST_RE.fullmatch(digest):
            raise EnvironmentError("Dependency SHA256 is required")
        cache = self.directory / "downloads"
        cache.mkdir(exist_ok=True)
        destination = cache / digest
        if destination.is_file() and file_digest(destination) == digest:
            return destination
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as sink:
            temporary = Path(sink.name)
            try:
                parsed = urllib.parse.urlparse(source)
                if parsed.scheme in {"https", "file"}:
                    with urllib.request.urlopen(source, timeout=60) as response:
                        shutil.copyfileobj(response, sink)
                else:
                    with Path(source).open("rb") as handle:
                        shutil.copyfileobj(handle, sink)
                sink.flush()
                if file_digest(temporary) != digest:
                    raise EnvironmentError("Downloaded dependency SHA256 mismatch")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def _exec(self, generation: dict[str, Any], argv: list[str], *, cwd: str | None = None) -> str:
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise EnvironmentError("Trusted environment commands must be nonempty string arrays")
        command = [self.docker, "exec"]
        for key, value in generation["env"].items():
            command.extend(["--env", f"{key}={value}"])
        if cwd:
            command.extend(["--workdir", cwd])
        return self._run([*command, generation["container"], *argv])

    def _prepare_llvm(self, generation: dict[str, Any], recipe: dict[str, Any]) -> None:
        llvm = recipe["llvm"]
        revision = generation["llvm_hash"]
        destination = Path(generation["workspace_host"]) / "deps" / f"llvm-{revision}"
        container_destination = generation["env"]["LLVM_BUILD_DIR"]
        mode = llvm.get("mode")
        provenance = {"llvm_hash": revision, "recipe_fingerprint": fingerprint(llvm), "mode": mode}
        cache_key = fingerprint({"llvm": llvm, "revision": revision, "image": generation.get("image_id")})
        cache_root = self.directory / "llvm-cache"
        cache_root.mkdir(exist_ok=True)
        cache = cache_root / cache_key
        if cache.is_dir():
            try:
                marker = json.loads((cache / "ready.json").read_text())
                valid = marker.get("key") == cache_key and marker.get("llvm_hash") == revision and tree_digest(cache / "install") == marker.get("tree_digest")
            except (OSError, ValueError, EnvironmentError):
                valid = False
            if valid:
                shutil.copytree(cache / "install", destination, symlinks=True)
                self._exec(generation, ["test", "-f", container_destination + "/lib/cmake/mlir/MLIRConfig.cmake"])
                self._exec(generation, [container_destination + "/bin/llvm-config", "--version"])
                self._exec(generation, [container_destination + "/bin/mlir-opt", "--version"])
                return
            # Keep corrupted material for diagnosis, then rebuild into a new
            # content-addressed ready entry. Neither location is container-mounted.
            os.replace(cache, cache_root / (cache_key + ".invalid-" + uuid.uuid4().hex[:8]))
        if mode == "archive":
            archive = self._download(str(llvm.get("archive", llvm.get("url", ""))), str(llvm.get("sha256", "")))
            if llvm.get("commit") != revision:
                raise EnvironmentError("Trusted LLVM archive provenance must name the requested exact commit")
            extract_verified_archive(archive, destination, llvm["sha256"], int(llvm.get("strip_components", 1)))
            provenance["sha256"] = llvm["sha256"]
        elif mode == "source":
            source = safe_source(llvm.get("repository"), "LLVM source mirror")
            source_dir = Path(generation["workspace_host"]) / "deps" / "llvm-source"
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git", "clone", "--no-checkout", "--", source, str(source_dir)])
            self._run(["git", "-C", str(source_dir), "checkout", "--detach", revision])
            actual = self._run(["git", "-C", str(source_dir), "rev-parse", "HEAD"]).strip()
            if actual != revision:
                raise EnvironmentError("LLVM source mirror did not provide the exact requested revision")
            for patch in llvm.get("patches", []):
                patch_path = absolute_path(patch.get("path"), "LLVM patch")
                if file_digest(patch_path) != patch.get("sha256"):
                    raise EnvironmentError("LLVM patch checksum mismatch")
                self._run(["git", "-C", str(source_dir), "apply", "--check", str(patch_path)])
                self._run(["git", "-C", str(source_dir), "apply", str(patch_path)])
            args = llvm.get("cmake_args", ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_ENABLE_ASSERTIONS=ON",
                                                   "-DLLVM_ENABLE_PROJECTS=mlir;clang;lld", "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU"])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise EnvironmentError("LLVM CMake arguments must be a trusted string array")
            prefix = generation["workspace_container"] + "/deps"
            self._exec(generation, ["cmake", "-S", prefix + "/llvm-source/llvm", "-B", prefix + "/llvm-build",
                                    *args, "-DCMAKE_INSTALL_PREFIX=" + container_destination])
            self._exec(generation, ["cmake", "--build", prefix + "/llvm-build", "--target", "install", "--parallel",
                                    str(recipe.get("build_jobs", 8))])
        else:
            raise EnvironmentError("New generations require llvm.mode archive or source")
        # These facts validate a toolchain, rather than a nonempty directory.
        self._exec(generation, ["test", "-f", container_destination + "/lib/cmake/mlir/MLIRConfig.cmake"])
        self._exec(generation, [container_destination + "/bin/llvm-config", "--version"])
        self._exec(generation, [container_destination + "/bin/mlir-opt", "--version"])
        atomic_json(destination / "local-ci-provenance.json", provenance)
        staging = cache_root / ("." + cache_key + ".preparing-" + uuid.uuid4().hex[:8])
        staging.mkdir()
        try:
            shutil.copytree(destination, staging / "install", symlinks=True)
            atomic_json(staging / "ready.json", {"key": cache_key, "llvm_hash": revision, "tree_digest": tree_digest(staging / "install"), "created_at": utc_now()})
            os.replace(staging, cache)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _prepare_repositories(self, generation: dict[str, Any], recipe: dict[str, Any]) -> None:
        for name, entry in recipe.get("repositories", {}).items():
            if not NAME_RE.fullmatch(name) or not isinstance(entry, dict):
                raise EnvironmentError("Repository recipe is invalid")
            source = safe_source(entry.get("repository"), f"{name} source mirror")
            revision = entry.get("commit", "")
            if not SHA_RE.fullmatch(revision):
                raise EnvironmentError(f"{name} must have a trusted exact commit")
            destination = Path(generation["workspace_host"]) / name
            self._run(["git", "clone", "--no-checkout", "--", source, str(destination)])
            self._run(["git", "-C", str(destination), "checkout", "--detach", revision])
            if self._run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip() != revision:
                raise EnvironmentError(f"{name} checkout SHA mismatch")

    def _new_generation(self, state: dict[str, Any], branch: str, profile: str, recipe: dict[str, Any], *, daily: bool) -> dict[str, Any]:
        root = absolute_path(recipe.get("workspace_root"), "Profile workspace_root").resolve()
        root.mkdir(parents=True, exist_ok=True)
        minimum = int(recipe.get("minimum_free_bytes", self.config.get("minimum_free_bytes", 10 * 1024**3)))
        if shutil.disk_usage(root).free < minimum:
            raise EnvironmentError("Insufficient free disk space to prepare a candidate environment")
        generation_id = f"{profile}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        workspace_host = root / generation_id
        workspace_host.mkdir()
        container_workspace = str(recipe.get("workspace_container", "/workspace"))
        if not container_workspace.startswith("/") or container_workspace == "/":
            raise EnvironmentError("workspace_container must be an absolute non-root path")
        revision = recipe["requested_llvm_hash"]
        env = {str(key): str(value) for key, value in recipe.get("env", {}).items()}
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) for key in env):
            raise EnvironmentError("Profile environment variable name is invalid")
        if any(any(word in key for word in ("TOKEN", "API_KEY", "PASSWORD", "SECRET", "CODEX_HOME")) for key in env):
            raise EnvironmentError("Candidate environment must not receive model or publishing credentials")
        env.update({"WORKSPACE": container_workspace, "LLVM_BUILD_DIR": f"{container_workspace}/deps/llvm-{revision}",
                    "LLVM_SYSPATH": f"{container_workspace}/deps/llvm-{revision}", "LOCAL_CI_LLVM_HASH": revision,
                    "RUN_BACKEND_STAGES": "true" if recipe.get("backend_enabled", False) else "false"})
        generation = {"profile": profile, "target_branch": branch, "generation": generation_id, "container": "anchor-ci-" + generation_id,
                      "workspace_host": str(workspace_host), "workspace_container": container_workspace, "llvm_hash": revision,
                      "backend_enabled": recipe.get("backend_enabled", False), "env": env, "state": "preparing", "created_at": utc_now(),
                      "recipe_fingerprint": fingerprint(recipe), "external": False, "daily_validation": daily}
        state["generations"][generation_id] = generation
        self._event(state, "generation_preparing", generation=generation_id, profile=profile)
        self._save(state)
        try:
            image = recipe.get("image")
            if not isinstance(image, str) or not image:
                raise EnvironmentError("Trusted profile image is required; no backend image is guessed")
            control_root = absolute_path(self.config.get("control_root"), "control_root").resolve()
            if not (control_root / "scripts/local_ci").is_dir():
                raise EnvironmentError("control_root does not contain trusted Local CI scripts")
            args = [self.docker, "create", "--name", generation["container"], "--restart", "unless-stopped",
                    "--label", "triton-anchor.role=persistent-ci", "--label", f"triton-anchor.generation={generation_id}",
                    "--mount", f"type=bind,source={workspace_host},target={container_workspace}",
                    "--mount", f"type=bind,source={control_root},target=/opt/local-ci/control,readonly",
                    "--mount", f"type=bind,source={control_root / 'scripts/local_ci/tools'},target=/opt/local-ci/tools,readonly"]
            for mount in recipe.get("mounts", []):
                source = absolute_path(mount.get("source"), "Trusted mount source")
                target = mount.get("target", "")
                if not isinstance(target, str) or not target.startswith("/") or target in {"/", "/var/run/docker.sock", "/run/docker.sock"}:
                    raise EnvironmentError("Trusted mount target is invalid")
                if mount.get("readonly", True) is not True:
                    raise EnvironmentError("Extra dependency mounts must be read-only")
                args.extend(["--mount", f"type=bind,source={source},target={target},readonly"])
            for device in recipe.get("devices", []):
                if not isinstance(device, str) or not device.startswith("/dev/"):
                    raise EnvironmentError("Device entries must be trusted /dev paths")
                args.extend(["--device", device])
            self._run([*args, "--entrypoint", "/bin/sh", image, "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"])
            self._run([self.docker, "start", generation["container"]])
            inspected = self._inspect(generation["container"])
            generation["container_id"] = inspected.get("Id", "")
            generation["image_id"] = inspected.get("Image", "")
            self._verify_container(generation)
            self._execution_identity(generation, recipe)
            self._prepare_llvm(generation, recipe)
            self._prepare_repositories(generation, recipe)
            for name, dependency in recipe.get("archives", {}).items():
                if not NAME_RE.fullmatch(name):
                    raise EnvironmentError("Dependency archive name is invalid")
                archive = self._download(str(dependency.get("archive", dependency.get("url", ""))), str(dependency.get("sha256", "")))
                dependency_root = workspace_host / "deps" / name
                extract_verified_archive(archive, dependency_root, dependency["sha256"], int(dependency.get("strip_components", 1)))
                atomic_json(dependency_root / "local-ci-provenance.json", {"name": name, "sha256": dependency["sha256"]})
            generation["environment_fingerprint"] = fingerprint({"recipe": recipe, "image_id": generation["image_id"], "llvm_hash": revision})
            generation["env"]["LOCAL_CI_ENVIRONMENT_FINGERPRINT"] = generation["environment_fingerprint"]
            for command in recipe.get("prepare_commands", []):
                self._exec(generation, command)
            validations = recipe.get("validation_commands", {})
            if daily:
                required = {"environment", "frontend_build", "wheel_install_import", "frontend_smoke"}
                if generation["backend_enabled"]:
                    required.update({"backend_rebuild", "backend_smoke_jit"})
                if not isinstance(validations, dict) or not required.issubset(validations):
                    raise EnvironmentError("Daily candidate recipe lacks required frontend/backend validation commands")
                order = ["environment", "frontend_build", "wheel_install_import", "frontend_smoke", "backend_rebuild", "backend_smoke_jit", "flaggems", "compile_time", "pass_profile", "ir_serialization"]
                for name in [*order, *[name for name in validations if name not in order]]:
                    if name in validations:
                        self._exec(generation, validations[name])
            self._runtime_access(generation, managed=True)
            generation["state"] = "ready"
            generation["ready_at"] = utc_now()
            generation["environment_fingerprint"] = fingerprint({"recipe": recipe, "image_id": generation["image_id"], "llvm_hash": revision})
            atomic_json(workspace_host / "environment.json", generation)
            self._event(state, "generation_ready", generation=generation_id, profile=profile)
            self._save(state)
            return generation
        except BaseException as exc:
            generation["state"] = "failed"
            generation["failure"] = str(exc)
            self._event(state, "generation_failed", generation=generation_id, profile=profile, reason=str(exc))
            self._save(state)
            if generation.get("container_id"):
                # Stopping the host Docker client alone leaves container
                # processes alive. This candidate is not leased or promoted,
                # so stop this exact owned generation while retaining it.
                try:
                    subprocess.run([self.docker, "stop", "--time", "10", generation["container"]], text=True,
                                   capture_output=True, timeout=30) if self.runner is subprocess.run else self.runner(
                                       [self.docker, "stop", "--time", "10", generation["container"]], text=True, capture_output=True, timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            # Keep the failed managed generation for diagnostics; never prune broadly.
            raise

    def _ensure(self, state: dict[str, Any], branch: str, revision: str) -> dict[str, Any]:
        profile, recipe = self._profile(branch, revision)
        digest = fingerprint(recipe)
        active = state["generations"].get(state["active"].get(branch, ""))
        candidates = ([active] if active else []) + list(reversed(list(state["generations"].values())))
        for generation in candidates:
            if generation["target_branch"] == branch and generation["llvm_hash"] == revision and generation["recipe_fingerprint"] == digest and generation["state"] in {"ready", "active", "previous"}:
                self._verify_container(generation)
                return generation
        existing = recipe.get("existing_container")
        if existing and revision == recipe["llvm_hash"] and not state["active"].get(branch):
            if not isinstance(existing, str) or not NAME_RE.fullmatch(existing):
                raise EnvironmentError("existing_container name is invalid")
            workspace = absolute_path(recipe.get("existing_workspace_host"), "existing_workspace_host")
            container_workspace = recipe.get("workspace_container", "/workspace")
            info = self._inspect(existing)
            matching_mount = any(mount.get("Destination") == container_workspace and Path(mount.get("Source", "")).resolve() == workspace.resolve() for mount in info.get("Mounts", []))
            if not matching_mount or not info.get("State", {}).get("Running"):
                raise EnvironmentError("Existing container is not running with its declared workspace mount")
            env = {str(key): str(value) for key, value in recipe.get("env", {}).items()}
            if not env.get("LLVM_BUILD_DIR"):
                raise EnvironmentError("Existing container needs its actual LLVM_BUILD_DIR; no default is assumed")
            env.update({"WORKSPACE": container_workspace, "LOCAL_CI_LLVM_HASH": revision,
                        "RUN_BACKEND_STAGES": "true" if recipe.get("backend_enabled") else "false"})
            generation_id = profile + "-adopted-" + fingerprint(info.get("Id", existing))[:12]
            generation = {"profile": profile, "target_branch": branch, "generation": generation_id, "container": existing,
                          "container_id": info.get("Id"), "workspace_host": str(workspace), "workspace_container": container_workspace,
                          "llvm_hash": revision, "backend_enabled": recipe.get("backend_enabled", False), "env": env,
                          "recipe_fingerprint": digest, "environment_fingerprint": fingerprint({"recipe": recipe, "image": info.get("Image"), "id": info.get("Id")}),
                          "external": True, "state": "active", "created_at": utc_now(), "daily_validation": False}
            self._exec(generation, [env["LLVM_BUILD_DIR"] + "/bin/llvm-config", "--version"])
            self._execution_identity(generation, recipe)
            self._runtime_access(generation, managed=False)
            state["generations"][generation_id] = generation
            state["active"][branch] = generation_id
            self._event(state, "generation_adopted", generation=generation_id, profile=profile)
            self._save(state)
            return generation
        generation = self._new_generation(state, branch, profile, recipe, daily=False)
        if revision == recipe["llvm_hash"] and branch not in state["active"]:
            state["active"][branch] = generation["generation"]
            generation["state"] = "active"
            self._save(state)
        return generation

    def ensure(self, target_branch: str, llvm_hash: str) -> dict[str, Any]:
        with self._locked():
            return copy.deepcopy(self._ensure(self._load(), target_branch, llvm_hash))

    def acquire(self, task_id: str, target_branch: str, llvm_hash: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
            raise EnvironmentError("task_id must be a nonempty bounded identifier")
        with self._locked():
            state = self._load()
            lease = state["leases"].get(task_id)
            if lease:
                generation = state["generations"][lease["generation"]]
                if generation["target_branch"] != target_branch or generation["llvm_hash"] != llvm_hash:
                    raise EnvironmentError("An existing task lease cannot change version or LLVM revision")
                self._verify_container(generation)
            else:
                generation = self._ensure(state, target_branch, llvm_hash)
                state["leases"][task_id] = {"generation": generation["generation"], "acquired_at": utc_now()}
                self._event(state, "lease_acquired", task_id=task_id, generation=generation["generation"])
                self._save(state)
            return copy.deepcopy(generation)

    def release(self, task_id: str) -> None:
        with self._locked():
            state = self._load()
            lease = state["leases"].pop(task_id, None)
            if lease:
                self._event(state, "lease_released", task_id=task_id, generation=lease["generation"])
                self._save(state)

    def rotate(self, target_branch: str) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            raw = self.config.get("profiles", {}).get(target_branch, {})
            profile, recipe = self._profile(target_branch, str(raw.get("llvm_hash", "")))
            generation = self._new_generation(state, target_branch, profile, recipe, daily=True)
            previous_id = state["active"].get(target_branch)
            if previous_id:
                state["generations"][previous_id]["state"] = "previous"
            generation["state"] = "active"
            state["active"][target_branch] = generation["generation"]
            self._event(state, "generation_promoted", generation=generation["generation"], previous=previous_id, profile=profile)
            self._save(state)
            return copy.deepcopy(generation)

    def rollback(self, target_branch: str) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            previous = [entry for entry in state["generations"].values() if entry["target_branch"] == target_branch and entry["state"] == "previous"]
            if not previous:
                raise EnvironmentError("No retained previous generation is available")
            generation = previous[-1]
            self._verify_container(generation)
            active_id = state["active"].get(target_branch)
            if active_id:
                state["generations"][active_id]["state"] = "ready"
            generation["state"] = "active"
            state["active"][target_branch] = generation["generation"]
            self._event(state, "generation_rollback", generation=generation["generation"], previous=active_id)
            self._save(state)
            return copy.deepcopy(generation)

    def collect_retired(self) -> dict[str, Any]:
        """Collect only owned, unused generations, preserving the latest fallback."""
        with self._locked():
            state = self._load()
            protected = set(state["active"].values()) | {lease["generation"] for lease in state["leases"].values()}
            for branch in self.config.get("profiles", {}):
                previous = [row for row in state["generations"].values() if row["target_branch"] == branch and row["state"] == "previous"]
                if previous:
                    protected.add(previous[-1]["generation"])
            removed = []
            grace = float(self.config.get("generation_retention_hours", 72)) * 3600
            for generation in list(state["generations"].values()):
                identifier = generation["generation"]
                if identifier in protected or generation.get("external") or generation["state"] == "preparing":
                    continue
                created = datetime.fromisoformat(generation["created_at"].replace("Z", "+00:00")).timestamp()
                if time.time() - created < grace:
                    continue
                info = self._inspect(generation["container"])
                if (info.get("Config", {}).get("Labels", {}) or {}).get("triton-anchor.generation") != identifier:
                    raise EnvironmentError("Refusing to remove a container with mismatched generation ownership")
                recipe = self.config["profiles"][generation["target_branch"]]
                root = absolute_path(recipe.get("workspace_root"), "workspace_root").resolve()
                workspace = Path(generation["workspace_host"]).resolve()
                if workspace.parent != root or workspace.name != identifier:
                    raise EnvironmentError("Refusing to remove a workspace outside its configured generation root")
                self._run([self.docker, "stop", "--time", "30", generation["container"]])
                self._run([self.docker, "rm", generation["container"]])
                if workspace.exists():
                    shutil.rmtree(workspace)
                del state["generations"][identifier]
                removed.append(identifier)
                self._event(state, "generation_collected", generation=identifier)
                self._save(state)
            return {"removed": removed, "protected": sorted(protected)}

    def health(self) -> dict[str, Any]:
        # registry.json is atomically replaced. Health must remain observable
        # while a source LLVM build holds the long-lived resource lock.
        state = self._load()
        rows = []
        leased = {lease["generation"] for lease in state["leases"].values()}
        for generation in state["generations"].values():
            row = {key: generation.get(key) for key in ("profile", "generation", "container", "target_branch", "llvm_hash", "state", "created_at")}
            row["leased"] = generation["generation"] in leased
            try:
                self._verify_container(generation)
                row["running"] = True
            except EnvironmentError as exc:
                row.update(running=False, error=str(exc))
            rows.append(row)
        return {"schema": SCHEMA, "collected_at": utc_now(), "active": state["active"], "generations": rows, "leases": state["leases"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("command", choices=("ensure", "rotate", "rollback", "health", "acquire", "release", "collect"))
    parser.add_argument("--target-branch")
    parser.add_argument("--llvm-hash")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    try:
        manager = EnvironmentManager(json.loads(Path(args.config).read_text(encoding="utf-8")), args.state_dir)
        if args.command == "collect":
            result = manager.collect_retired()
        elif args.command == "health":
            result = manager.health()
        elif args.command == "release":
            if not args.task_id:
                parser.error("--task-id is required")
            manager.release(args.task_id)
            result = {"released": args.task_id}
        elif args.command in {"rotate", "rollback"}:
            if not args.target_branch:
                parser.error("--target-branch is required")
            result = getattr(manager, args.command)(args.target_branch)
        else:
            if not args.target_branch or not args.llvm_hash:
                parser.error("--target-branch and --llvm-hash are required")
            if args.command == "acquire":
                if not args.task_id:
                    parser.error("--task-id is required")
                result = manager.acquire(args.task_id, args.target_branch, args.llvm_hash)
            else:
                result = manager.ensure(args.target_branch, args.llvm_hash)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EnvironmentError, OSError, ValueError) as exc:
        print(f"Environment operation failed: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
