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
from .dependency_mounts import (
    dependency_mounts,
    mount_arguments,
    validate_mounted_llvm,
    verify_mounts,
)
from .control_mount import (
    CONTROL_TARGET,
    snapshot_control,
    verify_snapshot,
    mount_arguments as control_mount_arguments,
    verify_mount as verify_control_mount,
)

SCHEMA = "triton-anchor-local-ci-environments/v2"
HELPER = "/opt/local-ci/control/scripts/local_ci/ops_maint/container_fs.py"
DEFAULT_IDENTITIES = dict(task=11001, gid=11001)
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
    if set(values) != {"task", "gid"} or any(
        type(v) is not int or not 0 < v < 65536 for v in values.values()
    ):
        raise EnvironmentError("Configure one non-root task UID and GID")
    return {"task": values["task"]}, {"task": values["gid"]}


def validate_branch_profiles(config):
    mappings = config.get("branch_profiles", {})
    profiles = config.get("profiles", {})
    if not isinstance(mappings, dict) or not isinstance(profiles, dict):
        raise EnvironmentError(
            "branch_profiles must map task branches to configured profile keys"
        )
    for branch, profile in mappings.items():
        if (
            not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,254}", branch)
            or branch == "CI_dev_forPR"
            or branch in profiles
            or not isinstance(profile, str)
            or profile not in profiles
            or profile == "CI_dev_forPR"
            or not isinstance(profiles[profile], dict)
        ):
            raise EnvironmentError(
                "branch_profiles needs explicit non-excluded aliases to existing profiles; "
                "chains and profile overrides are forbidden"
            )
    return dict(mappings)


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
                "Unsupported environment registry; use fresh v4 state"
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
        mounts = dependency_mounts(self.config, profile, verify_content=True)
        validate_mounted_llvm({**profile, "requested_llvm_hash": revision}, mounts)
        profile.update(
            target_branch=branch,
            requested_llvm_hash=revision,
            name=profile.get("name", branch.replace("/", "-")),
        )
        if not NAME_RE.fullmatch(profile["name"]):
            raise EnvironmentError("Unsafe profile name")
        profile["control_revision"] = self._control_revision()
        return profile

    def _control_revision(self):
        if self.config.get("container_control_root", CONTROL_TARGET) != CONTROL_TARGET:
            raise EnvironmentError("Container control root must be " + CONTROL_TARGET)
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
                "scripts",
                "envsetup.sh",
                "api_contract",
                ".github",
            ]
        )
        if not SHA_RE.fullmatch(sha) or dirty:
            raise EnvironmentError(
                "Runtime must use the clean committed control revision"
            )
        return sha

    def _control_snapshot(self, revision):
        return snapshot_control(
            self.config["control_root"],
            revision,
            self.directory / "control-revisions",
            self._run,
        )

    def _recipe_digest(self, profile):
        dependency_recipe = {
            k: v
            for k, v in profile.items()
            if k not in {"control_revision", "validation_commands", "daily_calendar"}
        }
        builder = [
            file_digest(Path(__file__).with_name(name))
            for name in ("Dockerfile", "image_prepare.py")
        ]
        return fingerprint([dependency_recipe, self.uids, self.gids, builder])

    def _validation_digest(self, profile):
        return fingerprint(
            [profile["control_revision"], profile.get("validation_commands", {})]
        )

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
        elif llvm["mode"] == "mount":
            validate_mounted_llvm(profile, profile.get("mounts", []))
        else:
            raise EnvironmentError("LLVM mode must be archive, source or mount")
        for entry in dependency_mounts(self.config, profile):
            (payload / "deps" / Path(entry["target"]).name).mkdir()
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
        shutil.copyfile(
            Path(__file__).with_name("image_prepare.py"), root / "image_prepare.py"
        )
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
            LOCAL_CI_TRITON_VERSION=profile["triton_version"],
            RUN_BACKEND_STAGES="true" if profile.get("backend_enabled") else "false",
        )
        atomic_json(
            root / "image-recipe.json",
            {
                "llvm": profile["llvm"],
                "env": env,
                "build_jobs": profile.get("build_jobs", 8),
                "prepare_commands": profile.get("prepare_commands", []),
            },
        )
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
            "frontend_install",
            "frontend_smoke",
        ]
        if profile.get("backend_enabled"):
            order += ["backend_build", "backend_install", "backend_smoke"]
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
        control = self._control_snapshot(profile["control_revision"])
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
                *control_mount_arguments(control),
                *mount_arguments(
                    dependency_mounts(self.config, profile, verify_content=True)
                ),
                ident,
            )
            .decode()
            .strip()
        )
        self._validation_container = container
        try:
            verify_control_mount(self._inspect(container), control)
            verify_mounts(self._inspect(container), profile.get("mounts", []))
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
            dependency_mounts(self.config, profile, verify_content=True)
            verify_snapshot(control)
            return dict(
                control_revision=profile["control_revision"],
                checks=sorted(commands),
                imports="verified",
                ppl=bool(profile.get("backend_enabled")),
            )
        finally:
            self._stop_owned(container, kind="image-validation")
            self._docker("rm", container, cancellable=False)
            self._validation_container = None

    def _foundation_reference(self, profile):
        local = profile.get("local_image_tag")
        if local is None:
            return profile["image"]
        if not isinstance(local, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", local
        ):
            raise EnvironmentError(
                "Offline foundation needs an explicit local image tag"
            )
        expected = self._inspect(profile["image"], True).get("Id")
        if not expected or self._inspect(local, True).get("Id") != expected:
            raise EnvironmentError(
                "Local foundation tag does not match the pinned image digest"
            )
        return local

    def _revalidate_image(self, row, profile, state):
        if row.get("validation_digest") == self._validation_digest(profile):
            return
        logs = self.directory / "image-logs"
        logs.mkdir(exist_ok=True)
        self._image_log = logs / ("revalidate-" + uuid.uuid4().hex + ".log")
        self._image_log.touch(mode=0o600)
        row["validation_log_path"] = str(self._image_log)
        self._save(state)
        try:
            proof = self._validate_image(row["image_id"], profile, row["env"])
            row.update(
                validation=proof,
                validated_at=utc_now(),
                validation_digest=self._validation_digest(profile),
            )
        except BaseException:
            if getattr(self, "_validation_container", None):
                row.update(
                    validation_container_id=self._validation_container,
                    validation_cleanup_confirmed=False,
                )
            self._save(state, "image_revalidation_failed", release_id=row["release_id"])
            raise
        finally:
            self._image_log = None

    def ensure_image(self, target_branch, llvm_hash, *, force=False):
        with self._lock(True), self._lock():
            daemon = self._daemon()
            state = self._load()
            self._safe(state)
            profile = self._profile(target_branch, llvm_hash)
            digest = self._recipe_digest(profile)
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
                    self._revalidate_image(row, profile, state)
                    self._save(
                        state,
                        "image_reused",
                        release_id=row["release_id"],
                        control_revision=profile["control_revision"],
                    )
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
                control_delivery="snapshot-mount",
                dependency_mounts=copy.deepcopy(profile.get("mounts", [])),
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
                    foundation = self._foundation_reference(profile)
                    self._docker(
                        "build",
                        "--pull=false",
                        "--iidfile",
                        str(root / "image.id"),
                        "--label",
                        "local-ci.owner=" + self.owner,
                        "--label",
                        "local-ci.kind=image",
                        "--build-arg",
                        "BASE_IMAGE=" + foundation,
                        str(root),
                    )
                    self._foundation_reference(profile)
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
                        validation_digest=self._validation_digest(profile),
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
            force=False,
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
                or self._recipe_digest(profile) != row.get("recipe_digest")
            ):
                raise EnvironmentError(
                    "Rollback requires a validated image with the current dependency recipe"
                )
            info = self._inspect(row["image_id"], True)
            if (
                info.get("Config", {}).get("Labels", {}).get("local-ci.owner")
                != self.owner
            ):
                raise EnvironmentError("Rollback image ownership differs")
            self._revalidate_image(row, profile, state)
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
        revision = self._control_revision()
        if task.get("worker_revision_sha") != revision:
            raise EnvironmentError(
                "Task worker revision differs from installed control"
            )
        profile_branch = validate_branch_profiles(self.config).get(
            task["target_branch"], task["target_branch"]
        )
        image = self.ensure_image(profile_branch, task["llvm_hash"])
        with self._lock(True), self._lock():
            state = self._load()
            self._safe(state)
            lease = state["leases"].get(task_id)
            if lease and state["attempts"][lease["generation"]]["state"] not in {
                "removed",
                "stopped",
                "lost",
            }:
                raise EnvironmentError("Task already has a live run")
            work = self.state_dir / "work" / task_id / run_id
            artifacts = self.state_dir / "runs" / task_id / run_id / "artifacts"
            rpc = Path(rpc_directory).resolve() if rpc_directory else work / "rpc"
            if not rpc.is_relative_to(work):
                raise EnvironmentError(
                    "RPC socket must live within the task work directory"
                )
            for path in (work, artifacts, rpc):
                path.mkdir(parents=True, exist_ok=True)
                # Container namespace UID 0 creates children for the task user.
                path.chmod(0o777)
            control = self._control_snapshot(revision)
            ident = run_id
            name = "local-ci-task-" + uuid.uuid4().hex
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
                )
            }
            handle.update(
                control_revision=revision,
                control_snapshot=control,
                dependency_mounts=copy.deepcopy(image.get("dependency_mounts", [])),
                target_branch=task["target_branch"],
                profile_branch=profile_branch,
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
                execution_uid=self.uids["task"],
                execution_gid=self.gids["task"],
                execution_user=str(self.uids["task"]) + ":" + str(self.gids["task"]),
                workspace_host=str(work),
                artifacts_host=str(artifacts),
                workspace_container="/task",
                rpc_host_dir=str(rpc),
                rpc_container_dir="/task/" + rpc.relative_to(work).as_posix(),
            )
            state["attempts"][ident] = handle
            state["leases"][task_id] = dict(
                generation=ident,
                run_id=run_id,
                image_release_id=image["image_release_id"],
            )
            self._save(state, "attempt_creating", attempt_id=ident)
            try:
                handle["container_id"] = (
                    self._docker(
                        "create",
                        "--name",
                        name,
                        "--user",
                        handle["execution_user"],
                        "--read-only",
                        "--security-opt",
                        "no-new-privileges=true",
                        "--tmpfs",
                        "/tmp:rw,nosuid,nodev,mode=1777",
                        *self._limits(),
                        "--label",
                        "local-ci.owner=" + self.owner,
                        "--label",
                        "local-ci.kind=task",
                        "--label",
                        "local-ci.attempt=" + ident,
                        "--mount",
                        "type=bind,source=" + str(work) + ",target=/task",
                        "--mount",
                        "type=bind,source="
                        + str(artifacts)
                        + ",target=/task/artifacts",
                        *control_mount_arguments(control),
                        *mount_arguments(handle["dependency_mounts"]),
                        image["image_id"],
                    )
                    .decode()
                    .strip()
                )
                self._save(state)
                self._verify(handle)
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
                    self._stop_owned(handle["container_id"], ident)
                    handle["state"] = "stopped"
                    self._save(state)
                raise

    def _record(self, handle):
        row = self._load()["attempts"].get(handle["attempt_id"])
        if not row or any(
            row.get(k) != handle.get(k)
            for k in (
                "task_id",
                "run_id",
                "image_id",
                "container_id",
                "control_revision",
            )
        ):
            raise EnvironmentError("Task handle differs from runtime registry")
        return row

    def _verify(self, handle):
        self._record(handle)
        info = self._inspect(handle["container_id"])
        labels = info.get("Config", {}).get("Labels", {})
        if (
            info.get("Image") != handle["image_id"]
            or labels.get("local-ci.owner") != self.owner
            or labels.get("local-ci.attempt") != handle["attempt_id"]
        ):
            raise EnvironmentError("Task container identity differs")
        expected = {
            "/task": handle["workspace_host"],
            "/task/artifacts": handle["artifacts_host"],
        }
        mounts = {m["Destination"]: m for m in info.get("Mounts", [])}
        for target, source in expected.items():
            mount = mounts.get(target, {})
            if (
                mount.get("Type") != "bind"
                or mount.get("Source") != source
                or mount.get("RW") is not True
            ):
                raise EnvironmentError("Task writable mount identity differs")
        allowed = (
            set(expected)
            | {CONTROL_TARGET}
            | {m["target"] for m in handle["dependency_mounts"]}
        )
        if set(mounts) != allowed:
            raise EnvironmentError("Unexpected host mount in task container")
        verify_control_mount(info, handle["control_snapshot"])
        verify_mounts(info, handle["dependency_mounts"])
        return info

    def recover_task(self, handle):
        # A worker restart never takes over a half-installed execution environment.
        self.stop_task(handle)
        return dict(status="rebuild_required")

    def _helper(
        self, handle, op, params=None, *, input_bytes=None, binary=False, verify=True
    ):
        if verify:
            self._verify(handle)
        output = self._docker(
            "exec",
            "-i",
            "--user",
            "0:0",
            handle["container_id"],
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
            raise EnvironmentError("Unsupported file operation")
        return self._helper(handle, operation, params, input_bytes=input_bytes)

    def import_checkout(self, handle, variant, archive_path, sha256, expected_sha=None):
        path = Path(archive_path)
        if path.is_symlink() or file_digest(path) != sha256:
            raise EnvironmentError("Frozen source archive checksum mismatch")
        return self._helper(
            handle,
            "import-checkout",
            dict(
                variant=variant,
                sha256=sha256,
                expected_sha=expected_sha
                or handle["task"]["base_sha" if variant == "base" else "tested_sha"],
            ),
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
        return dict(authorized=True)

    def create_experiment(self, h, experiment_id, variant="candidate"):
        return self._helper(
            h, "create-experiment", dict(experiment_id=experiment_id, variant=variant)
        )

    def prepare_workspace(self, h, variant="candidate"):
        return self._helper(
            h,
            "prepare-workspace",
            dict(variant=variant, environment_fingerprint=h["environment_fingerprint"]),
        )

    def prepare_native_workspace(self, h):
        return {**self.prepare_workspace(h), "source_sha": h["task"]["tested_sha"]}

    def read_file(self, h, scope, path):
        return base64.b64decode(
            self._helper(h, "read-file", dict(scope=scope, path=path))["base64"]
        ).decode()

    def _export(self, h, op, params, destination):
        output = self._helper(h, op, params, binary=True)
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(output), mode="r:") as archive:
            for member in archive:
                path = Path(member.name)
                if not member.isfile() or path.is_absolute() or ".." in path.parts:
                    raise EnvironmentError("Unsafe evidence export")
                if path.name in {"execution.log", "executor-record.json"}:
                    continue
                target = root / path
                if any(p.is_symlink() for p in (target, *target.parents)):
                    raise EnvironmentError("Symlinked evidence destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.extractfile(member).read()
                if target.exists() and target.read_bytes() != data:
                    raise EnvironmentError("Export would change saved evidence")
                target.write_bytes(data)
        return dict(exported=True, destination=str(root))

    def export_execution(self, h, execution_id, destination):
        if (
            Path(destination).resolve()
            == (Path(h["artifacts_host"]) / execution_id).resolve()
        ):
            self._helper(h, "make-artifacts-readable", dict(execution_id=execution_id))
            return dict(exported=True, destination=str(destination))
        return self._export(
            h, "export-execution", dict(execution_id=execution_id), destination
        )

    def export_evidence(self, h, destination):
        # Artifacts are already durable from command start; export is needed only
        # for a different host destination used by a caller.
        if Path(destination).resolve() == Path(h["artifacts_host"]).resolve():
            return dict(exported=True, destination=str(destination))
        return self._export(h, "export-evidence", {}, destination)

    def export_native_evidence(self, h, destination):
        return self._export(h, "export-native-evidence", {}, destination)

    def deploy_session(self, h, files, environment):
        layout = dict(
            home="/task/session/home",
            workspace="/task/candidate/checkout",
            python_bin=self.config.get("container_python", "python3"),
            mcp_script="/opt/local-ci/control/scripts/local_ci/agent_ci/mcp_server.py",
            rpc_socket=h["rpc_container_dir"] + "/broker.sock",
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
        return self._helper(h, "task-usage")["bytes"]

    def _resolve_task_container(self, h):
        """Reconcile a create whose response was lost using its exact owned name."""
        row = self._record(h)
        present = (
            self._docker("ps", "-aq", "--no-trunc", cancellable=False)
            .decode()
            .splitlines()
        )
        if row.get("container_id"):
            return row["container_id"] if row["container_id"] in present else None
        matches = (
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
        if not matches:
            return None
        if len(matches) != 1:
            raise EnvironmentError("Ambiguous task container creation")
        info = self._inspect(matches[0])
        labels = info.get("Config", {}).get("Labels", {})
        if (
            info.get("Image") != row["image_id"]
            or labels.get("local-ci.owner") != self.owner
            or labels.get("local-ci.kind") != "task"
            or labels.get("local-ci.attempt") != h["attempt_id"]
        ):
            raise EnvironmentError(
                "Unconfirmed create resolved to an unowned container"
            )
        h["container_id"] = matches[0]
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]]["container_id"] = matches[0]
            self._save(state, "attempt_creation_reconciled", attempt_id=h["attempt_id"])
        return matches[0]

    def _remove_scratch(self, h):
        work = Path(h["workspace_host"])
        expected = self.state_dir / "work" / h["task_id"] / h["run_id"]
        if not work.is_absolute() or work != expected or work.resolve() != expected:
            raise EnvironmentError("Task work cleanup escaped its configured path")
        name = "local-ci-cleanup-" + fingerprint([h["task_id"], h["run_id"]])[:24]
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
        if len(existing) > 1:
            raise EnvironmentError("Ambiguous scratch cleanup container")
        if existing:
            self._stop_owned(existing[0], h["attempt_id"], kind="task-cleanup")
            self._docker("rm", existing[0], cancellable=False)
        if not work.exists():
            return
        try:
            shutil.rmtree(work)
            return
        except PermissionError:
            # Rootless subuid-owned files may be unreadable to the host account.
            # An inert, offline management invocation clears only this work bind.
            pass
        control = h["control_snapshot"]
        ident = (
            self._docker(
                "create",
                "--name",
                name,
                "--user",
                "0:0",
                "--network",
                "none",
                "--read-only",
                "--security-opt",
                "no-new-privileges=true",
                *self._limits(),
                "--label",
                "local-ci.owner=" + self.owner,
                "--label",
                "local-ci.kind=task-cleanup",
                "--label",
                "local-ci.attempt=" + h["attempt_id"],
                "--mount",
                "type=bind,source=" + str(work) + ",target=/task",
                *control_mount_arguments(control),
                "--entrypoint",
                self.config.get("container_python", "python3"),
                h["image_id"],
                "-I",
                "-S",
                "-B",
                HELPER,
                "clean-work",
                '{"scratch_only":true}',
                cancellable=False,
            )
            .decode()
            .strip()
        )
        try:
            self._docker(
                "start",
                "--attach",
                ident,
                cancellable=False,
                timeout=self.config.get("management_timeout_seconds", 600),
            )
        finally:
            self._stop_owned(ident, h["attempt_id"], kind="task-cleanup")
            self._docker("rm", ident, cancellable=False)
        shutil.rmtree(work)

    def stop_task(self, h):
        row = self._record(h)
        if row["state"] == "removed":
            return dict(verified=True, stopped=True, remaining=[])
        ident = self._resolve_task_container(h)
        if ident:
            result = self._stop_owned(ident, row["attempt_id"])
        else:
            result = dict(
                verified=True, stopped=True, remaining=[], container_missing=True
            )
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]].update(
                state="stopped", stopped=True, stopped_at=utc_now()
            )
            self._save(state, "attempt_stopped", attempt_id=h["attempt_id"])
        return result

    def destroy_task(self, h, *, keep_data=False):
        row = self._record(h)
        if row["state"] == "removed":
            return dict(removed=True, stopped=True)
        if self._daemon() != row["daemon_id"]:
            raise EnvironmentError("Rootless daemon identity changed during cleanup")
        ident = self._resolve_task_container(h)
        if ident:
            self._verify(h)
            if not self._inspect(ident).get("State", {}).get("Running"):
                # Only the fixed inert entrypoint resumes; never prior Agent work.
                self._docker("start", ident, cancellable=False)
            self.purge_credentials(h)
            self._helper(h, "clean-work")
            self.stop_task(h)
            self._docker("rm", ident, cancellable=False)
        self._remove_scratch(h)
        # Persistent artifacts are never removed by container cleanup.
        with self._lock():
            state = self._load()
            state["attempts"][h["attempt_id"]].update(state="removed", stopped=True)
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
                "removed",
                "lost",
            }:
                raise EnvironmentError("Cannot release a live task")
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
        usage, usage_available = [], True
        active = [
            a["container_id"]
            for a in attempts
            if a.get("state") == "running" and a.get("container_id")
        ]
        if active:
            try:
                raw = self._docker(
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    *active,
                    timeout=30,
                )
                for line in raw.decode().splitlines():
                    row = json.loads(line)
                    usage.append(
                        {
                            "container_id": row.get("ID", row.get("Container", "")),
                            "cpu_percent": float(row["CPUPerc"].rstrip("%")),
                            "memory_percent": float(row["MemPerc"].rstrip("%")),
                            "pids": int(row["PIDs"]),
                        }
                    )
            except (EnvironmentError, ValueError, KeyError):
                usage_available = False
        return dict(
            schema=SCHEMA,
            collected_at=utc_now(),
            images=list(copy.deepcopy(state["images"]).values()),
            attempts=attempts,
            generations=attempts,
            active_images=state["active_images"],
            leases=state["leases"],
            runtime=copy.deepcopy(self.config["runtime"]),
            resource_usage=usage,
            resource_usage_available=usage_available,
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
