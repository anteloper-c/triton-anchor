"""Rootless Docker lifecycle. File operations never require host chown."""

from __future__ import annotations
import argparse
import base64
import contextlib
import copy
from datetime import datetime
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from .artifacts import (
    EnvironmentError,
    SHA_RE,
    DIGEST_RE,
    NAME_RE,
    atomic_json,
    utc_now,
    fingerprint,
    file_digest,
    safe_source,
    extract_verified_archive,
)

SCHEMA = "triton-anchor-local-ci-environments/v2"
HELPER = "/opt/local-ci/control/scripts/local_ci/environments/container_fs.py"
DEFAULT_IDENTITIES = dict(
    candidate=11001,
    base=11002,
    diagnostic=11003,
    codex=11004,
    read_gid=11000,
    codex_gid=11004,
)
IMAGE_RE = re.compile(r"(?:[^\s]+@)?sha256:[a-f0-9]{64}")


def docker_command(config, *args):
    runtime = config.get("runtime", {})
    endpoint = runtime.get("endpoint", "")
    if (
        runtime.get("kind") != "docker-rootless"
        or not endpoint.startswith("unix:///")
        or ".." in Path(endpoint[7:]).parts
        or any(c in endpoint for c in "\n\r\x00")
    ):
        raise EnvironmentError("A fixed unix rootless Docker endpoint is required")
    return [config.get("docker_bin", "docker"), "--host", endpoint, *args]


def identities(config):
    values = {**DEFAULT_IDENTITIES, **config.get("identities", {})}
    uids = {role: values[role] for role in ("candidate", "base", "diagnostic", "codex")}
    if (
        len(set(uids.values())) != 4
        or any(
            type(value) is not int or not 0 < value < 65536 for value in values.values()
        )
        or values["codex_gid"] == values["read_gid"]
    ):
        raise EnvironmentError(
            "Four distinct non-root UIDs and a separate Codex group are required"
        )
    return uids, {
        role: values["codex_gid"] if role == "codex" else values["read_gid"]
        for role in uids
    }


class EnvironmentManager:
    def __init__(self, config, state_dir, runner=None):
        self.config, self.state_dir = config, Path(state_dir).resolve()
        self.directory = self.state_dir / "environments"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry = self.directory / "registry.json"
        self.runner, self.cancel_event = runner or subprocess.run, None
        self.prefix = docker_command(config)
        self.owner = fingerprint([str(self.state_dir), self.prefix[-1]])[:24]
        self.uids, self.gids = identities(config)

    def docker_command(self, *args):
        return [*self.prefix, *args]

    @contextlib.contextmanager
    def _lock(self, resource=False):
        with (
            self.state_dir / ("resource.lock" if resource else "environments.lock")
        ).open("a") as stream:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise EnvironmentError("Environment operation cancelled")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _load(self):
        if not self.registry.exists():
            return dict(
                schema=SCHEMA,
                images={},
                active_images={},
                attempts={},
                leases={},
                events=[],
            )
        try:
            state = json.loads(self.registry.read_text())
        except (OSError, ValueError):
            raise EnvironmentError("Environment registry is unreadable") from None
        if state.get("schema") != SCHEMA:
            raise EnvironmentError(
                "Drain legacy rootful environments before using a fresh rootless state"
            )
        return state

    def _save(self, state, event=None, **details):
        if event:
            item = dict(at=utc_now(), event=event, **details)
            state["events"] = [*state["events"], item][-200:]
            with (self.directory / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(item) + "\n")
        atomic_json(self.registry, state)

    def _run(self, args, *, input_bytes=None, timeout=None, cancellable=True):
        timeout = timeout or self.config.get("environment_command_timeout", 14400)
        if cancellable and self.cancel_event is not None and self.cancel_event.is_set():
            raise EnvironmentError("Environment operation cancelled")
        image_log, live_log = getattr(self, "_image_log", None), None
        try:
            if self.runner is not subprocess.run:
                result = self.runner(
                    args, input=input_bytes, capture_output=True, timeout=timeout
                )
            else:
                if (
                    image_log
                    and args[: len(self.prefix)] == self.prefix
                    and args[len(self.prefix)] in {"build", "exec"}
                ):
                    live_log = image_log.open("ab", buffering=0)
                    live_log.write(
                        (
                            "\n["
                            + utc_now()
                            + "] docker "
                            + args[len(self.prefix)]
                            + " started\n"
                        ).encode()
                    )
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE
                    if input_bytes is not None
                    else subprocess.DEVNULL,
                    stdout=live_log or subprocess.PIPE,
                    stderr=subprocess.STDOUT if live_log else subprocess.PIPE,
                    start_new_session=True,
                )
                started, payload = time.monotonic(), input_bytes
                while True:
                    if (
                        time.monotonic() - started > timeout
                        or cancellable
                        and self.cancel_event is not None
                        and self.cancel_event.is_set()
                    ):
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.communicate()
                        raise EnvironmentError(
                            "Environment operation cancelled or timed out"
                            + ("; image log: " + str(image_log) if image_log else "")
                        )
                    try:
                        stdout, stderr = proc.communicate(input=payload, timeout=0.25)
                        result = subprocess.CompletedProcess(
                            args, proc.returncode, stdout, stderr
                        )
                        break
                    except subprocess.TimeoutExpired:
                        payload = None
        except (OSError, subprocess.TimeoutExpired):
            raise EnvironmentError("Environment subprocess could not finish") from None
        finally:
            if live_log:
                os.fsync(live_log.fileno())
                live_log.close()
        if image_log:
            with image_log.open("ab") as stream:
                stream.write(
                    (
                        "\n["
                        + utc_now()
                        + "] "
                        + Path(args[0]).name
                        + " exit="
                        + str(result.returncode)
                        + "\n"
                    ).encode()
                )
                for output in (result.stdout, result.stderr):
                    stream.write(
                        output.encode() if isinstance(output, str) else output or b""
                    )
                stream.flush()
                os.fsync(stream.fileno())
        if result.returncode:
            raise EnvironmentError(
                "Environment subprocess failed (exit "
                + str(result.returncode)
                + ")"
                + ("; image log: " + str(image_log) if image_log else "")
            )
        output = result.stdout or b""
        return output.encode() if isinstance(output, str) else output

    def _docker(self, *args, **kw):
        return self._run(self.docker_command(*args), **kw)

    def _daemon(self):
        if self.runner is subprocess.run:
            if os.geteuid() == 0:
                raise EnvironmentError(
                    "Rootless Harness requires an ordinary host CI account"
                )
            info = Path(self.prefix[-1][7:]).stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise EnvironmentError(
                    "Docker endpoint must be a socket owned by the CI account"
                )
        info = json.loads(self._docker("info", "--format", "{{json .}}", timeout=30))
        if not info.get("ID") or not any(
            "rootless" in str(x) for x in info.get("SecurityOptions", [])
        ):
            raise EnvironmentError(
                "Docker daemon is not rootless; no rootful fallback is allowed"
            )
        if info.get("CgroupVersion") != "2" or info.get("CgroupDriver") != "systemd":
            raise EnvironmentError(
                "Rootless resource limits require cgroup v2 with systemd"
            )
        return info["ID"]

    def _limits(self):
        values = self.config.get("resources", {})
        if (
            type(values.get("cpus")) not in (int, float)
            or not math.isfinite(values["cpus"])
            or values["cpus"] <= 0
            or any(
                type(values.get(k)) is not int or values[k] <= 0
                for k in ("memory_bytes", "pids_limit")
            )
        ):
            raise EnvironmentError("Explicit CPU, memory and PID limits are required")
        return [
            "--cpus",
            str(values["cpus"]),
            "--memory",
            str(values["memory_bytes"]),
            "--pids-limit",
            str(values["pids_limit"]),
        ]

    def _inspect(self, ident, image=False):
        rows = json.loads(
            self._docker(
                *(["image", "inspect"] if image else ["inspect"]),
                ident,
                timeout=30,
                cancellable=False,
            )
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise EnvironmentError("Invalid Docker inspection")
        return rows[0]

    def _safe(self, state):
        if any(
            row.get("validation_cleanup_confirmed") is False
            for row in state["images"].values()
        ):
            raise EnvironmentError(
                "Image validation container stop is unconfirmed; run collect after Docker recovers"
            )
        if any(row["state"] == "unsafe" for row in state["attempts"].values()):
            raise EnvironmentError(
                "Task container stop is unconfirmed; new work is blocked"
            )

    def _profile(self, branch, revision):
        profile = copy.deepcopy(self.config.get("profiles", {}).get(branch))
        if not isinstance(profile, dict) or not SHA_RE.fullmatch(revision):
            raise EnvironmentError(
                "Trusted profile and exact LLVM revision are required"
            )
        if not IMAGE_RE.fullmatch(profile.get("image", "")):
            raise EnvironmentError("Foundation image must use an immutable digest")
        if profile.get("backend_enabled") and str(
            profile.get("triton_version", "")
        ).split(".")[:2] != ["3", "0"]:
            raise EnvironmentError("Only Triton 3.0 may enable the deployed backend")
        if str(profile.get("triton_version", "")).split(".")[:2] == [
            "3",
            "0",
        ] and not profile.get("backend_enabled"):
            raise EnvironmentError(
                "Triton 3.0 requires the configured backend capability"
            )
        llvm = profile["llvm"]
        if revision != profile["llvm_hash"]:
            if revision in llvm.get("revisions", {}):
                llvm.update(llvm["revisions"][revision])
            elif llvm.get("mode") != "source":
                raise EnvironmentError(
                    "New LLVM requires a trusted archive or source recipe"
                )
        llvm.pop("revisions", None)
        profile.update(
            target_branch=branch,
            requested_llvm_hash=revision,
            name=profile.get("name", branch.replace("/", "-")),
        )
        if not NAME_RE.fullmatch(profile["name"]):
            raise EnvironmentError("Unsafe profile name")
        root = self.config["control_root"]
        sha = self._run(["git", "-C", root, "rev-parse", "HEAD"]).decode().strip()
        dirty = self._run(
            [
                "git",
                "-C",
                root,
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                "scripts/local_ci",
                "scripts/ci",
                "api_contract",
                ".github",
            ]
        )
        if not SHA_RE.fullmatch(sha) or dirty:
            raise EnvironmentError(
                "Image recipes must use the clean committed control revision"
            )
        profile["control_revision"] = sha
        return profile

    def _download(self, source, digest):
        safe_source(source, "Dependency")
        if not DIGEST_RE.fullmatch(digest):
            raise EnvironmentError("Dependency SHA256 is mandatory")
        root = self.directory / "downloads"
        root.mkdir(exist_ok=True)
        target = root / digest
        if target.is_file() and file_digest(target) == digest:
            return target
        temp = root / (".incoming-" + uuid.uuid4().hex)
        try:
            if urllib.parse.urlparse(source).scheme:
                with (
                    urllib.request.urlopen(source, timeout=60) as response,
                    temp.open("wb") as stream,
                ):
                    shutil.copyfileobj(response, stream)
            else:
                shutil.copyfile(source, temp)
            if file_digest(temp) != digest:
                raise EnvironmentError("Dependency SHA256 mismatch")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return target

    def _checkout(self, source, sha, target):
        safe_source(source, "Repository")
        if not SHA_RE.fullmatch(sha):
            raise EnvironmentError("Repository commit must be exact")
        self._run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--no-checkout",
                "--",
                source,
                str(target),
            ]
        )
        self._run(
            [
                "git",
                "-C",
                str(target),
                "-c",
                "core.hooksPath=/dev/null",
                "checkout",
                "--detach",
                sha,
            ]
        )
        if (
            self._run(["git", "-C", str(target), "rev-parse", "HEAD"]).decode().strip()
            != sha
        ):
            raise EnvironmentError("Repository commit mismatch")

    def _build_context(self, profile, root):
        payload = root / "payload"
        (payload / "deps").mkdir(parents=True)
        llvm, sha = profile["llvm"], profile["requested_llvm_hash"]
        if llvm["mode"] == "archive":
            if llvm.get("commit") != sha:
                raise EnvironmentError("LLVM archive provenance mismatch")
            archive = self._download(
                llvm.get("archive", llvm.get("url", "")), llvm.get("sha256", "")
            )
            extract_verified_archive(
                archive,
                payload / "deps" / ("llvm-" + sha),
                llvm["sha256"],
                llvm.get("strip_components", 1),
            )
        elif llvm["mode"] == "source":
            target = payload / "deps/llvm-source"
            self._checkout(llvm["repository"], sha, target)
            for patch in llvm.get("patches", []):
                path = Path(patch["path"])
                if not path.is_absolute() or file_digest(path) != patch["sha256"]:
                    raise EnvironmentError("LLVM patch checksum mismatch")
                self._run(["git", "-C", str(target), "apply", "--check", str(path)])
                self._run(["git", "-C", str(target), "apply", str(path)])
        else:
            raise EnvironmentError("LLVM mode must be archive or source")
        for name, entry in profile.get("archives", {}).items():
            if not NAME_RE.fullmatch(name):
                raise EnvironmentError("Unsafe dependency name")
            archive = self._download(
                entry.get("archive", entry.get("url", "")), entry.get("sha256", "")
            )
            extract_verified_archive(
                archive,
                payload / "deps" / name,
                entry["sha256"],
                entry.get("strip_components", 1),
            )
        for name, entry in profile.get("repositories", {}).items():
            if not NAME_RE.fullmatch(name) or name == "deps":
                raise EnvironmentError("Unsafe repository directory")
            self._checkout(entry["repository"], entry["commit"], payload / name)
        control = Path(self.config["control_root"])
        shutil.copytree(
            control / "scripts",
            root / "control/scripts",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in ("envsetup.sh", "api_contract"):
            source = control / name
            if source.is_dir():
                shutil.copytree(source, root / "control" / name)
            elif source.is_file():
                shutil.copyfile(source, root / "control" / name)
        shutil.copyfile(Path(__file__).with_name("Dockerfile"), root / "Dockerfile")
        env, old = {}, profile.get("workspace_container", "/workspace")
        for key, value in profile.get("env", {}).items():
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or any(
                x in key
                for x in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CODEX_HOME")
            ):
                raise EnvironmentError("Credentials cannot enter image recipes")
            env[key] = (
                "/opt/local-ci/runtime" + value[len(old) :]
                if isinstance(value, str) and value.startswith(old + "/")
                else str(value)
            )
        env.update(
            WORKSPACE="/opt/local-ci/runtime",
            LLVM_BUILD_DIR="/opt/local-ci/runtime/deps/llvm-" + sha,
            LLVM_SYSPATH="/opt/local-ci/runtime/deps/llvm-" + sha,
            LOCAL_CI_LLVM_HASH=sha,
            RUN_BACKEND_STAGES="true" if profile.get("backend_enabled") else "false",
        )
        atomic_json(root / "image-recipe.json", {**profile, "env": env})
        return env

    def _stop_owned(self, ident, attempt=None, kind="task"):
        info = self._inspect(ident)
        labels = info.get("Config", {}).get("Labels", {})
        if (
            info.get("Id") != ident
            or labels.get("local-ci.owner") != self.owner
            or labels.get("local-ci.kind") != kind
            or attempt
            and labels.get("local-ci.attempt") != attempt
        ):
            raise EnvironmentError("Refusing to stop an unowned container")
        if info.get("State", {}).get("Running"):
            self._docker("stop", "--time", "10", ident, timeout=30, cancellable=False)
        if self._inspect(ident).get("State", {}).get("Running") is not False:
            raise EnvironmentError("Container stop is unconfirmed")
        return dict(verified=True, stopped=True, remaining=[])

    def _validate_image(self, ident, profile, env):
        order = [
            "environment",
            "frontend_build",
            "wheel_install_import",
            "frontend_smoke",
        ]
        if profile.get("backend_enabled"):
            order += ["backend_rebuild", "backend_smoke_jit"]
        commands = profile.get("validation_commands", {})
        if not set(order).issubset(commands) or any(
            not isinstance(x, list)
            or not x
            or not all(isinstance(arg, str) and arg for arg in x)
            or Path(x[0]).name in {"true", ":"}
            for x in commands.values()
        ):
            raise EnvironmentError(
                "Image release needs actual frontend/backend validation commands"
            )
        container = (
            self._docker(
                "create",
                "--name",
                "local-ci-validate-" + uuid.uuid4().hex,
                "--user",
                "0:0",
                "--label",
                "local-ci.owner=" + self.owner,
                "--label",
                "local-ci.kind=image-validation",
                *self._limits(),
                ident,
            )
            .decode()
            .strip()
        )
        self._validation_container = container
        try:
            self._docker("start", container)
            prefix = ["exec"]
            for key, value in env.items():
                prefix += ["--env", key + "=" + value]
            prefix += ["--env", "PYTHONDONTWRITEBYTECODE=1", container]
            seed = env.get("SEED_PYTHON") or str(
                Path(env.get("PYTHON_VENV_ACTIVATE", "/opt/venv/bin/activate")).parent
                / "python"
            )
            self._docker(
                *prefix,
                seed,
                "-I",
                "-c",
                "import build,setuptools,wheel,pybind11,yaml,pytest",
            )
            self._docker(
                *prefix,
                self.config.get("codex_bin", "/usr/local/bin/codex"),
                "--version",
            )
            if profile.get("backend_enabled"):
                self._docker(*prefix, "test", "-d", env.get("PPL_ROOT", "/missing-ppl"))
                setup = env.get("BACKEND_ENVSETUP")
                if setup:
                    import shlex

                    setup = (
                        setup
                        if setup.startswith("/")
                        else env["BACKEND_PATH"] + "/" + setup
                    )
                    self._docker(
                        *prefix,
                        "bash",
                        "-e",
                        "-c",
                        'source "$1" "${@:3}"; exec "$2" -I -c "import torch,torch_tpu"',
                        "probe",
                        setup,
                        seed,
                        *shlex.split(env.get("BACKEND_ENVSETUP_ARGS", "")),
                    )
                else:
                    self._docker(*prefix, seed, "-I", "-c", "import torch,torch_tpu")
            for check in [*order, *sorted(set(commands) - set(order))]:
                self._docker(*prefix, *commands[check])
            return dict(
                checks=sorted(commands),
                imports="verified",
                ppl=bool(profile.get("backend_enabled")),
            )
        finally:
            self._stop_owned(container, kind="image-validation")
            self._docker("rm", container, cancellable=False)
            self._validation_container = None

    def ensure_image(self, target_branch, llvm_hash, *, force=False):
        with self._lock(True), self._lock():
            daemon = self._daemon()
            state = self._load()
            self._safe(state)
            profile = self._profile(target_branch, llvm_hash)
            digest = fingerprint([profile, self.uids, self.gids])
            active_id = state["active_images"].get(target_branch)
            candidates = sorted(
                state["images"].values(),
                key=lambda item: (
                    item["release_id"] == active_id,
                    item["created_at"],
                    item["release_id"],
                ),
                reverse=True,
            )
            for row in candidates:
                if (
                    not force
                    and row["recipe_digest"] == digest
                    and row["validated"]
                    and row.get("daemon_id") == daemon
                    and row["state"] != "quarantined"
                ):
                    self._inspect(row["image_id"], True)
                    return copy.deepcopy(row)
            release = digest[:24] + "-" + uuid.uuid4().hex[:8]
            row = dict(
                release_id=release,
                image_release_id=release,
                profile=profile["name"],
                target_branch=target_branch,
                llvm_hash=llvm_hash,
                recipe_digest=digest,
                validated=False,
                state="preparing",
                created_at=utc_now(),
                backend_enabled=bool(profile.get("backend_enabled")),
                daemon_id=daemon,
                control_revision=profile["control_revision"],
            )
            state["images"][release] = row
            self._save(state, "image_preparing", release_id=release)
            logs = self.directory / "image-logs"
            logs.mkdir(exist_ok=True)
            self._image_log = logs / (release + ".log")
            self._image_log.touch(mode=0o600)
            row["log_path"] = str(self._image_log)
            with tempfile.TemporaryDirectory(
                prefix="build-", dir=self.directory
            ) as directory:
                root = Path(directory)
                try:
                    env = self._build_context(profile, root)
                    self._docker(
                        "build",
                        "--iidfile",
                        str(root / "image.id"),
                        "--label",
                        "local-ci.owner=" + self.owner,
                        "--label",
                        "local-ci.kind=image",
                        "--build-arg",
                        "BASE_IMAGE=" + profile["image"],
                        str(root),
                    )
                    image_id = (root / "image.id").read_text().strip()
                    if not IMAGE_RE.fullmatch(image_id):
                        raise EnvironmentError(
                            "Build produced no immutable image identity"
                        )
                    row.update(image_id=image_id, env=env, state="validating")
                    self._save(state)
                    proof = self._validate_image(image_id, profile, env)
                    row.update(
                        state="ready",
                        validated=True,
                        validated_at=utc_now(),
                        validation=proof,
                        environment_fingerprint=fingerprint([digest, image_id]),
                    )
                    if llvm_hash == self.config["profiles"][target_branch]["llvm_hash"]:
                        previous = state["active_images"].get(target_branch)
                        if previous:
                            state.setdefault("previous_images", {})[target_branch] = (
                                previous
                            )
                        state["active_images"][target_branch] = release
                    self._save(state, "image_ready", release_id=release)
                    return copy.deepcopy(row)
                except BaseException:
                    row["state"] = "failed"
                    if getattr(self, "_validation_container", None):
                        row.update(
                            validation_container_id=self._validation_container,
                            validation_cleanup_confirmed=False,
                        )
                    self._save(state, "image_failed", release_id=release)
                    raise
                finally:
                    self._image_log = None

    def rotate(self, target_branch):
        return self.ensure_image(
            target_branch,
            self.config["profiles"][target_branch]["llvm_hash"],
            force=True,
        )

    def import_foundation(self, archive, sha256, image_ref):
        """Import an administrator-provided offline foundation, not a PR snapshot.

        This is provenance only. A release must still be built from the trusted
        recipe and pass ensure_image validation before any task may acquire it.
        """
        if not IMAGE_RE.fullmatch(image_ref):
            raise EnvironmentError(
                "Imported foundation requires an immutable image identity"
            )
        with self._lock(True), self._lock():
            self._daemon()
            state = self._load()
            self._safe(state)
            path = self._download(archive, sha256)
            self._docker("load", "--input", str(path))
            image = self._inspect(image_ref, True)
            if not IMAGE_RE.fullmatch(image.get("Id", "")):
                raise EnvironmentError("Imported foundation identity is unavailable")
            record = dict(
                archive_sha256=sha256,
                image_ref=image_ref,
                image_id=image["Id"],
                imported_at=utc_now(),
                validated=False,
            )
            state.setdefault("foundations", {})[sha256] = record
            self._save(
                state,
                "foundation_imported",
                image_id=image["Id"],
                archive_sha256=sha256,
            )
            return record

    def rollback_image(self, target_branch, release_id):
        with self._lock(True), self._lock():
            daemon = self._daemon()
            state = self._load()
            self._safe(state)
            row = state["images"].get(release_id)
            profile = self._profile(
                target_branch, self.config["profiles"][target_branch]["llvm_hash"]
            )
            if (
                not row
                or not row.get("validated")
                or row.get("state") != "ready"
                or row.get("daemon_id") != daemon
                or row.get("recipe_digest")
                != fingerprint([profile, self.uids, self.gids])
            ):
                raise EnvironmentError(
                    "Rollback requires a validated image with the current profile, LLVM and control recipe"
                )
            info = self._inspect(row["image_id"], True)
            if (
                info.get("Config", {}).get("Labels", {}).get("local-ci.owner")
                != self.owner
            ):
                raise EnvironmentError("Rollback image ownership differs")
            previous = state["active_images"].get(target_branch)
            if previous and previous != release_id:
                state.setdefault("previous_images", {})[target_branch] = previous
            state["active_images"][target_branch] = release_id
            self._save(state, "image_rollback", release_id=release_id)
            return copy.deepcopy(row)

    def acquire_task(self, task, run_id, *, rpc_directory=None):
        task_id = task["task_id"]
        if not re.fullmatch(r"[a-f0-9]{64}", task_id) or not NAME_RE.fullmatch(run_id):
            raise EnvironmentError("Invalid task/run identity")
        state = self._load()
        lease = state["leases"].get(task_id)
        if lease and lease["run_id"] == run_id:
            result = self.recover_task(state["attempts"][lease["generation"]])
            if result["status"] == "same_attempt":
                return result["handle"]
        image = self.ensure_image(task["target_branch"], task["llvm_hash"])
        with self._lock(True), self._lock():
            state = self._load()
            self._safe(state)
            ident, name = uuid.uuid4().hex, "local-ci-task-" + uuid.uuid4().hex
            host = self.state_dir / "task-staging" / task_id / ident
            host.mkdir(parents=True)
            rpc = (
                Path(rpc_directory) if rpc_directory else self.state_dir / "rpc" / ident
            )
            if not rpc.is_absolute() or rpc.resolve() != rpc:
                raise EnvironmentError(
                    "RPC directory must be an absolute non-symlink task path"
                )
            rpc.mkdir(parents=True, exist_ok=True)
            rpc.chmod(0o711)
            handle = {
                key: copy.deepcopy(image[key])
                for key in (
                    "profile",
                    "image_release_id",
                    "image_id",
                    "llvm_hash",
                    "backend_enabled",
                    "env",
                    "environment_fingerprint",
                    "daemon_id",
                    "control_revision",
                )
            }
            handle.update(
                target_branch=task["target_branch"],
                task_id=task_id,
                run_id=run_id,
                task=task,
                attempt_id=ident,
                generation=ident,
                container=name,
                container_id=None,
                state="creating",
                created_at=utc_now(),
                uids=self.uids,
                gids=self.gids,
                workspace_host=str(host),
                workspace_container="/task",
                rpc_host_dir=str(rpc),
                rpc_container_dir="/run/local-ci-rpc",
                execution_uid=self.uids["candidate"],
                execution_gid=self.gids["candidate"],
                execution_user=str(self.uids["candidate"])
                + ":"
                + str(self.gids["candidate"]),
                volumes=dict(task=name + "-data", codex=name + "-session"),
            )
            state["attempts"][ident] = handle
            state["leases"][task_id] = dict(
                generation=ident, run_id=run_id, image_release_id=image["release_id"]
            )
            self._save(state, "attempt_creating", attempt_id=ident)
            try:
                for volume in handle["volumes"].values():
                    self._docker(
                        "volume",
                        "create",
                        "--label",
                        "local-ci.owner=" + self.owner,
                        "--label",
                        "local-ci.attempt=" + ident,
                        volume,
                    )
                args = [
                    "create",
                    "--name",
                    name,
                    "--user",
                    "0:0",
                    "--read-only",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,mode=1777",
                    "--tmpfs",
                    "/var/tmp:rw,nosuid,nodev,mode=1777",
                    *self._limits(),
                    "--label",
                    "local-ci.owner=" + self.owner,
                    "--label",
                    "local-ci.kind=task",
                    "--label",
                    "local-ci.attempt=" + ident,
                ]
                for source, target, kind, readonly in (
                    (handle["volumes"]["task"], "/task", "volume", False),
                    (handle["volumes"]["codex"], "/codex", "volume", False),
                    (str(rpc), "/run/local-ci-rpc", "bind", True),
                ):
                    args += [
                        "--mount",
                        "type="
                        + kind
                        + ",source="
                        + source
                        + ",target="
                        + target
                        + (",readonly" if readonly else ""),
                    ]
                handle["container_id"] = (
                    self._docker(*args, image["image_id"]).decode().strip()
                )
                self._save(state)
                self._docker("start", handle["container_id"])
                payload = {
                    key: handle[key]
                    for key in (
                        "task_id",
                        "run_id",
                        "attempt_id",
                        "uids",
                        "gids",
                        "env",
                        "backend_enabled",
                    )
                }
                payload["python_bin"] = self.config.get("container_python", "python3")
                self._helper(
                    handle,
                    "init",
                    input_bytes=json.dumps(payload).encode(),
                    verify=False,
                )
                handle["state"] = "running"
                self._save(state, "attempt_ready", attempt_id=ident)
                return copy.deepcopy(handle)
            except BaseException:
                handle["state"] = "unsafe"
                self._save(state, "attempt_creation_failed", attempt_id=ident)
                if handle["container_id"]:
                    try:
                        self._stop_owned(handle["container_id"], ident)
                        handle["state"] = "stopped"
                        self._save(state)
                    except EnvironmentError:
                        pass
                else:
                    # A create reply may have been lost: resolve only our exact
                    # generated name, then verify labels before stopping it.
                    try:
                        existing = (
                            self._docker(
                                "ps",
                                "-aq",
                                "--no-trunc",
                                "--filter",
                                "name=^/" + name + "$",
                                cancellable=False,
                            )
                            .decode()
                            .splitlines()
                        )
                        if existing:
                            if len(existing) != 1:
                                raise EnvironmentError(
                                    "Ambiguous task container creation"
                                )
                            handle["container_id"] = existing[0]
                            self._stop_owned(existing[0], ident)
                        handle["state"] = "stopped"
                        self._save(state)
                    except EnvironmentError:
                        pass
                raise

    def _record(self, handle):
        row = self._load()["attempts"].get(handle["attempt_id"])
        if (
            not row
            or any(
                row.get(k) != handle.get(k) for k in ("task_id", "run_id", "image_id")
            )
            or row
            and handle.get("container_id") is not None
            and row.get("container_id") != handle.get("container_id")
        ):
            raise EnvironmentError("Attempt handle differs from trusted registry")
        return row

    def _verify(self, handle):
        self._record(handle)
        if (
            not isinstance(handle.get("container_id"), str)
            or not handle["container_id"]
        ):
            raise EnvironmentError("Task container creation did not complete")
        info = self._inspect(handle["container_id"])
        labels = info.get("Config", {}).get("Labels", {})
        if (
            info.get("Id") != handle["container_id"]
            or info.get("Image") != handle["image_id"]
            or labels.get("local-ci.owner") != self.owner
            or labels.get("local-ci.attempt") != handle["attempt_id"]
        ):
            raise EnvironmentError("Task container identity or ownership changed")
        mounts = {m["Destination"]: m for m in info.get("Mounts", [])}
        expected = {
            "/task": ("volume", handle["volumes"]["task"], True),
            "/codex": ("volume", handle["volumes"]["codex"], True),
            "/run/local-ci-rpc": ("bind", handle["rpc_host_dir"], False),
        }
        for target, (kind, source, writable) in expected.items():
            mount = mounts.get(target, {})
            if (
                mount.get("Type") != kind
                or mount.get("Name" if kind == "volume" else "Source") != source
                or mount.get("RW") is not writable
            ):
                raise EnvironmentError("Task container mount identity changed")
        return info

    def recover_task(self, handle):
        handle = self._record(handle)
        if handle.get("state") in {"removed", "lost", "creating"} or not handle.get(
            "container_id"
        ):
            return dict(status="rebuild_required")
        if self._daemon() != handle["daemon_id"]:
            raise EnvironmentError("Rootless daemon identity changed")
        try:
            info = self._verify(handle)
        except EnvironmentError:
            ids = (
                self._docker("ps", "-aq", "--no-trunc", timeout=30)
                .decode()
                .splitlines()
            )
            if handle["container_id"] in ids:
                raise
            with self._lock():
                state = self._load()
                state["attempts"][handle["attempt_id"]]["state"] = "lost"
                self._save(state, "attempt_lost", attempt_id=handle["attempt_id"])
            return dict(status="rebuild_required")
        if not info.get("State", {}).get("Running"):
            self._docker("start", handle["container_id"])
        with self._lock():
            state = self._load()
            state["attempts"][handle["attempt_id"]]["state"] = "running"
            self._save(state)
        return dict(status="same_attempt", handle=copy.deepcopy(self._record(handle)))

    def _recovery_container(self, handle):
        """Reconstruct management access to lost-attempt volumes, never test state."""
        if self._daemon() != handle["daemon_id"]:
            raise EnvironmentError("Rootless daemon identity changed")
        with self._lock():
            state = self._load()
            row = state["attempts"][handle["attempt_id"]]
            if row.get("container_id") is None:
                # Docker create may complete just before the worker crashes
                # without persisting its response. Its generated name and
                # labels identify the inert orphan precisely.
                ids = (
                    self._docker(
                        "ps",
                        "-aq",
                        "--no-trunc",
                        "--filter",
                        "name=^/" + row["container"] + "$",
                        cancellable=False,
                    )
                    .decode()
                    .splitlines()
                )
                if len(ids) > 1:
                    raise EnvironmentError("Ambiguous half-created task container")
                if ids:
                    self._stop_owned(ids[0], row["attempt_id"])
                    row["container_id"] = ids[0]
                    self._save(
                        state,
                        "attempt_creation_reconciled",
                        attempt_id=row["attempt_id"],
                    )
            available = (
                self._docker("volume", "ls", "-q", cancellable=False)
                .decode()
                .splitlines()
            )
            if row["volumes"]["task"] not in available:
                row.update(state="lost", evidence_loss="task_volume_missing")
                self._save(
                    state,
                    "attempt_evidence_lost",
                    attempt_id=handle["attempt_id"],
                    reason="task_volume_missing",
                )
                raise EnvironmentError(
                    "Task evidence volume is missing; preserved host evidence remains authoritative"
                )
            has_codex = row["volumes"]["codex"] in available
            if not has_codex and not row.get("credential_volume_missing"):
                row["credential_volume_missing"] = True
                self._save(
                    state,
                    "attempt_credential_volume_lost",
                    attempt_id=row["attempt_id"],
                )
            for name in (name for name in row["volumes"].values() if name in available):
                info = json.loads(
                    self._docker("volume", "inspect", name, cancellable=False)
                )[0]
                if (
                    info.get("Labels", {}).get("local-ci.owner") != self.owner
                    or info.get("Labels", {}).get("local-ci.attempt")
                    != row["attempt_id"]
                ):
                    raise EnvironmentError("Recovery volume identity differs")
            present = (
                self._docker("ps", "-aq", "--no-trunc", cancellable=False)
                .decode()
                .splitlines()
            )
            ident = row.get("recovery_container_id")
            if ident not in present:
                args = [
                    "create",
                    "--name",
                    "local-ci-recover-" + row["attempt_id"],
                    "--user",
                    "0:0",
                    "--read-only",
                    "--network",
                    "none",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,mode=1777",
                    *self._limits(),
                    "--label",
                    "local-ci.owner=" + self.owner,
                    "--label",
                    "local-ci.kind=task-recovery",
                    "--label",
                    "local-ci.attempt=" + row["attempt_id"],
                    "--mount",
                    "type=volume,source="
                    + row["volumes"]["task"]
                    + ",target=/task,readonly",
                ]
                if has_codex:
                    args += [
                        "--mount",
                        "type=volume,source="
                        + row["volumes"]["codex"]
                        + ",target=/codex",
                    ]
                ident = (
                    self._docker(*args, row["image_id"], cancellable=False)
                    .decode()
                    .strip()
                )
                row.update(
                    state="lost", recovery_only=True, recovery_container_id=ident
                )
                self._save(
                    state, "attempt_management_recovered", attempt_id=row["attempt_id"]
                )
            info = self._inspect(ident)
            labels = info.get("Config", {}).get("Labels", {})
            if (
                info.get("Id") != ident
                or info.get("Image") != row["image_id"]
                or labels.get("local-ci.owner") != self.owner
                or labels.get("local-ci.kind") != "task-recovery"
                or labels.get("local-ci.attempt") != row["attempt_id"]
            ):
                raise EnvironmentError("Recovery container identity differs")
            mounts = {m["Destination"]: m for m in info.get("Mounts", [])}
            expected = [("/task", "task", False)] + (
                [("/codex", "codex", True)] if has_codex else []
            )
            if set(mounts) != {item[0] for item in expected} or any(
                mounts[target].get("Name") != row["volumes"][key]
                or mounts[target].get("RW") is not writable
                for target, key, writable in expected
            ):
                raise EnvironmentError("Recovery container mounts differ")
            return info

    def _helper(
        self, handle, op, params=None, *, input_bytes=None, binary=False, verify=True
    ):
        handle = self._record(handle)
        temporary_start, recovery = False, False
        container_id = handle["container_id"]
        read_operations = {
            "export-execution",
            "export-evidence",
            "export-native-evidence",
            "task-usage",
            "purge-credentials",
            "read-file",
        }
        if verify:
            try:
                info = self._verify(handle)
            except EnvironmentError:
                present = (
                    self._docker("ps", "-aq", "--no-trunc", cancellable=False)
                    .decode()
                    .splitlines()
                )
                if container_id in present or op not in read_operations:
                    raise
                info = self._recovery_container(handle)
                container_id, recovery = info["Id"], True
            if not info.get("State", {}).get("Running"):
                if op not in read_operations:
                    raise EnvironmentError(
                        "Stopped task permits only evidence export, usage and credential cleanup"
                    )
                self._docker("start", container_id, cancellable=False)
                temporary_start = True
        try:
            if op == "export-native-evidence" and self._record(handle).get(
                "credential_volume_missing"
            ):
                raise EnvironmentError("Native evidence Codex volume is missing")
            output = self._docker(
                "exec",
                "-i",
                "--user",
                "0:0",
                container_id,
                self.config.get("container_python", "python3"),
                "-I",
                "-S",
                "-B",
                HELPER,
                op,
                json.dumps(params or {}, separators=(",", ":")),
                input_bytes=input_bytes,
                timeout=self.config.get("management_timeout_seconds", 600),
                cancellable=False,
            )
        finally:
            if temporary_start or recovery:
                if recovery:
                    self._stop_owned(
                        container_id, handle["attempt_id"], kind="task-recovery"
                    )
                else:
                    self.stop_task(handle)
        return output if binary else json.loads(output)

    def fs_operation(self, handle, operation, params=None, input_bytes=None):
        if operation not in {
            "prepare-execution",
            "write-execution-file",
            "verify-checkout",
            "runtime-info",
            "authorize-diagnostics",
            "create-experiment",
            "write-baseline",
            "task-usage",
            "read-file",
        }:
            raise EnvironmentError("Unsupported fixed file operation")
        return self._helper(handle, operation, params, input_bytes=input_bytes)

    def import_checkout(self, handle, variant, archive_path, sha256, expected_sha=None):
        path = Path(archive_path)
        if path.is_symlink() or file_digest(path) != sha256:
            raise EnvironmentError("Frozen source archive checksum mismatch")
        expected_sha = (
            expected_sha
            or handle["task"]["base_sha" if variant == "base" else "tested_sha"]
        )
        return self._helper(
            handle,
            "import-checkout",
            dict(variant=variant, sha256=sha256, expected_sha=expected_sha),
            input_bytes=path.read_bytes(),
        )

    def prepare_execution(self, h, execution_id, variant, *, diagnostic=False):
        return self._helper(
            h,
            "prepare-execution",
            dict(execution_id=execution_id, variant=variant, diagnostic=diagnostic),
        )

    def write_execution_file(self, h, execution_id, name, content):
        return self._helper(
            h,
            "write-execution-file",
            dict(execution_id=execution_id, name=name),
            input_bytes=content.encode() if isinstance(content, str) else content,
        )

    def runtime_info(self, h):
        return self._helper(h, "runtime-info")

    def verify_checkout(self, h, variant, expected_sha):
        return self._helper(
            h, "verify-checkout", dict(variant=variant, expected_sha=expected_sha)
        )

    def write_baseline(self, h, tool_id, payload):
        return self._helper(
            h,
            "write-baseline",
            dict(tool_id=tool_id),
            input_bytes=json.dumps(payload).encode(),
        )

    def authorize_diagnostics(self, h):
        return self._helper(h, "authorize-diagnostics")

    def create_experiment(self, h, experiment_id, variant="candidate"):
        return self._helper(
            h, "create-experiment", dict(experiment_id=experiment_id, variant=variant)
        )

    def prepare_native_workspace(self, h):
        h = self._record(h)
        return self._helper(
            h, "prepare-native-workspace", dict(expected_sha=h["task"]["tested_sha"])
        )

    def read_file(self, h, scope, path):
        return base64.b64decode(
            self._helper(h, "read-file", dict(scope=scope, path=path))["base64"]
        ).decode()

    def _export(self, h, op, params, destination):
        try:
            output = self._helper(h, op, params, binary=True)
        except EnvironmentError:
            if self._record(h).get("evidence_loss") == "task_volume_missing":
                return dict(exported=False, evidence_loss="task_volume_missing")
            raise
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(output), mode="r:") as archive:
            total = 0
            for member in archive:
                path = Path(member.name)
                total += member.size
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or ".." in path.parts
                    or total > 512 * 1024**2
                ):
                    raise EnvironmentError("Unsafe evidence export")
                if path.name in {"execution.log", "executor-record.json"}:
                    continue
                target = root / path
                if any(p.is_symlink() for p in [target, *target.parents]):
                    raise EnvironmentError("Symlinked evidence destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.extractfile(member).read()
                if target.exists() and target.read_bytes() != data:
                    raise EnvironmentError("Export would change saved evidence")
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, delete=False
                ) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    temp = stream.name
                os.replace(temp, target)
        return dict(exported=True, destination=str(root))

    def export_execution(self, h, execution_id, destination):
        return self._export(
            h, "export-execution", dict(execution_id=execution_id), destination
        )

    def export_evidence(self, h, destination):
        return self._export(h, "export-evidence", {}, destination)

    def export_native_evidence(self, h, destination):
        """Private exploratory audit, deliberately separate from formal exports."""
        if self._record(h).get("credential_volume_missing"):
            return dict(exported=False, evidence_loss="codex_volume_missing")
        try:
            result = self._export(h, "export-native-evidence", {}, destination)
            if result.get("exported"):
                Path(destination).chmod(0o700)
            return result
        except EnvironmentError:
            if self._record(h).get("credential_volume_missing"):
                return dict(exported=False, evidence_loss="codex_volume_missing")
            raise

    def deploy_session(self, h, files, environment):
        layout = dict(
            home="/codex/home",
            workspace="/codex/workspace",
            python_bin=self.config.get("container_python", "python3"),
            mcp_script="/opt/local-ci/control/scripts/local_ci/agent_ci/mcp_server.py",
            rpc_socket="/run/local-ci-rpc/broker.sock",
        )
        if not files and not environment:
            return layout
        result = self._helper(
            h,
            "deploy-session",
            input_bytes=json.dumps(
                dict(
                    files=files,
                    environment={
                        **environment,
                        "LOCAL_CI_CODEX_BIN": self.config.get(
                            "codex_bin", "/usr/local/bin/codex"
                        ),
                    },
                )
            ).encode(),
        )
        return {**layout, **result}

    def purge_credentials(self, h):
        return self._helper(h, "purge-credentials")

    def task_usage(self, h):
        row = self._record(h)
        if row["state"] in {"stopped", "retained"} and "usage_bytes" in row:
            return row["usage_bytes"]
        try:
            value = self._helper(h, "task-usage")["bytes"]
        except EnvironmentError:
            if self._record(h).get("evidence_loss") != "task_volume_missing":
                raise
            value = 0
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]]["usage_bytes"] = value
            self._save(state)
        return value

    def stop_task(self, h):
        with self._lock():
            state = self._load()
            row = state["attempts"][h["attempt_id"]]
            if row["state"] == "removed":
                return dict(verified=True, stopped=True, remaining=[])
            present = (
                self._docker("ps", "-aq", "--no-trunc", cancellable=False)
                .decode()
                .splitlines()
            )
            if row.get("container_id") not in present:
                recovery_id = row.get("recovery_container_id")
                if recovery_id in present:
                    self._stop_owned(
                        recovery_id, row["attempt_id"], kind="task-recovery"
                    )
                row.update(state="lost", stopped=True)
                self._save(
                    state, "attempt_container_absent", attempt_id=row["attempt_id"]
                )
                return dict(
                    verified=True, stopped=True, remaining=[], container_missing=True
                )
            try:
                result = self._stop_owned(h["container_id"], h["attempt_id"])
                row.update(state="stopped", stopped=True, stopped_at=utc_now())
            except EnvironmentError:
                row.update(state="unsafe", stopped=False)
                self._save(
                    state, "attempt_stop_unconfirmed", attempt_id=h["attempt_id"]
                )
                raise
            self._save(state, "attempt_stopped", attempt_id=h["attempt_id"])
            return result

    def destroy_task(self, h, *, keep_data=True):
        row = self._record(h)
        h = row
        if row["state"] == "removed":
            return dict(removed=True, stopped=True)
        if self._daemon() != row["daemon_id"]:
            raise EnvironmentError("Rootless daemon identity changed during cleanup")
        existing = (
            self._docker("ps", "-aq", "--no-trunc", cancellable=False)
            .decode()
            .splitlines()
        )
        present = row.get("container_id") in existing
        if present:
            self._verify(h)
            self.purge_credentials(h)
            self.task_usage(h)
            self.stop_task(h)
        elif row.get("container_id") and row["state"] != "removing":
            # Missing container is distinct from missing evidence. Access the
            # old volumes only through a restricted, non-networked manager.
            try:
                self.purge_credentials(h)
                self.task_usage(h)
            except EnvironmentError:
                if not self._record(h).get("evidence_loss"):
                    raise
        if keep_data and present:
            with self._lock():
                state = self._load()
                row = state["attempts"][h["attempt_id"]]
                row["state"] = "retained"
                row.setdefault("retained_at", utc_now())
                self._save(state)
            return dict(retained=True, stopped=True)
        # A lost container cannot run the credential-purge helper. Delete its
        # two exclusively owned volumes instead of retaining unknown secrets.
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]]["state"] = "removing"
            self._save(state)
        if present:
            self._docker("rm", h["container_id"], cancellable=False)
        recovery_id = self._record(h).get("recovery_container_id")
        if (
            recovery_id
            and recovery_id
            in self._docker("ps", "-aq", "--no-trunc", cancellable=False)
            .decode()
            .splitlines()
        ):
            self._stop_owned(recovery_id, h["attempt_id"], kind="task-recovery")
            self._docker("rm", recovery_id, cancellable=False)
        known_volumes = (
            self._docker("volume", "ls", "-q", cancellable=False).decode().splitlines()
        )
        for volume in h["volumes"].values():
            if volume not in known_volumes:
                continue
            info = json.loads(
                self._docker("volume", "inspect", volume, cancellable=False)
            )[0]
            labels = info.get("Labels", {})
            if (
                labels.get("local-ci.owner") != self.owner
                or labels.get("local-ci.attempt") != h["attempt_id"]
            ):
                raise EnvironmentError("Refusing to delete an unowned volume")
            self._docker("volume", "rm", volume, cancellable=False)
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]]["state"] = "removed"
            if (
                state["leases"].get(h["task_id"], {}).get("generation")
                == h["attempt_id"]
            ):
                state["leases"].pop(h["task_id"])
            self._save(state, "attempt_removed", attempt_id=h["attempt_id"])
        return dict(removed=True, stopped=True)

    def release(self, task_id):
        with self._lock():
            state = self._load()
            lease = state["leases"].get(task_id)
            if lease and state["attempts"][lease["generation"]]["state"] not in {
                "stopped",
                "retained",
                "removed",
                "lost",
            }:
                raise EnvironmentError("Cannot release a live or unsafe attempt")
            state["leases"].pop(task_id, None)
            self._save(state)

    def leases(self):
        return copy.deepcopy(self._load()["leases"])

    def generations(self):
        return copy.deepcopy(self._load()["attempts"])

    def generation(self, ident):
        return self.generations()[ident]

    def collect_retired(self):
        with self._lock(True), self._lock():
            state = self._load()
            for row in state["images"].values():
                if row.get("validation_cleanup_confirmed") is False:
                    ident = row["validation_container_id"]
                    present = (
                        self._docker("ps", "-aq", "--no-trunc", cancellable=False)
                        .decode()
                        .splitlines()
                    )
                    if ident in present:
                        self._stop_owned(ident, kind="image-validation")
                        self._docker("rm", ident, cancellable=False)
                    row["validation_cleanup_confirmed"] = True
                    self._save(
                        state,
                        "image_validation_cleanup_confirmed",
                        release_id=row["release_id"],
                    )
            protected = set(state["active_images"].values()) | {
                x["image_release_id"]
                for x in state["attempts"].values()
                if x["state"] != "removed"
            }
            protected.update(state.get("previous_images", {}).values())
            removed = []
            for ident, row in list(state["images"].items()):
                age = (
                    time.time()
                    - datetime.fromisoformat(
                        row["created_at"].replace("Z", "+00:00")
                    ).timestamp()
                )
                if (
                    ident in protected
                    or age < self.config.get("generation_retention_hours", 72) * 3600
                    or not row.get("image_id")
                ):
                    continue
                if not any(
                    other != ident and item.get("image_id") == row["image_id"]
                    for other, item in state["images"].items()
                ):
                    info = self._inspect(row["image_id"], True)
                    if (
                        info.get("Config", {}).get("Labels", {}).get("local-ci.owner")
                        != self.owner
                    ):
                        raise EnvironmentError("Refusing to remove an unowned image")
                    self._docker("image", "rm", row["image_id"], cancellable=False)
                state["images"].pop(ident)
                removed.append(ident)
            self._save(state)
            return dict(removed=removed, protected=sorted(protected))

    def health(self):
        state = self._load()
        attempts = list(copy.deepcopy(state["attempts"]).values())
        return dict(
            schema=SCHEMA,
            collected_at=utc_now(),
            images=list(copy.deepcopy(state["images"]).values()),
            attempts=attempts,
            generations=attempts,
            active_images=state["active_images"],
            leases=state["leases"],
            runtime=copy.deepcopy(self.config["runtime"]),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument(
        "command", choices=("health", "rotate", "ensure", "collect", "rollback")
    )
    parser.add_argument("--target-branch")
    parser.add_argument("--llvm-hash")
    parser.add_argument("--release-id")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    manager = EnvironmentManager(config, args.state_dir or config["state_dir"])
    result = (
        manager.health()
        if args.command == "health"
        else manager.collect_retired()
        if args.command == "collect"
        else manager.rotate(args.target_branch)
        if args.command == "rotate"
        else manager.rollback_image(args.target_branch, args.release_id)
        if args.command == "rollback"
        else manager.ensure_image(args.target_branch, args.llvm_hash)
    )
    print(json.dumps(result, indent=2))
