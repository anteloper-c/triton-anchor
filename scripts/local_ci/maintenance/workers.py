"""One reusable Docker worker per trusted version profile; no task containers.

Profile dictionaries are server configuration, NEVER values supplied by a PR or
task manifest. A lease survives process failure: recovery must first stop the
orphan task before releasing its lease. Timeouts alone cannot prove it stopped.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone


class WorkerError(RuntimeError):
    pass


class WorkerBusy(WorkerError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def file_lock(path):
    """Nonblocking OS lock, also released by the OS after process termination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkerBusy(f"operation already active: {path.name}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def window_open(settings, now=None):
    """Daily window in UTC, with an explicit offset per version; wraps midnight."""
    now = now or datetime.now(timezone.utc)
    hour, minute = map(int, settings.get("window_start", "02:00").split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise WorkerError("invalid maintenance window_start")
    start = (hour * 60 + minute + int(settings.get("stagger_minutes", 0))) % 1440
    duration = int(settings.get("window_minutes", 120))
    if not 1 <= duration <= 1440:
        raise WorkerError("window_minutes must be in 1..1440")
    return (now.hour * 60 + now.minute - start) % 1440 < duration


class WorkerManager:
    def __init__(self, state_dir, docker="docker", run=subprocess.run):
        """Use trusted host state_dir; run is injectable for fault tests."""
        self.root = Path(state_dir) / "workers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.docker = docker
        self.run = run

    def _key(self, profile):
        key = profile["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", key):
            raise WorkerError("invalid profile id")
        container = profile["container"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", container["name"]):
            raise WorkerError("invalid fixed container name")
        if not container.get("image") or not container.get("healthcheck"):
            raise WorkerError("container image and healthcheck are required")
        return key

    def _path(self, profile):
        return self.root / (self._key(profile) + ".json")

    def _state(self, profile):
        return read_json(self._path(profile), {"profile_id": profile["id"], "draining": False, "lease": None})

    def _save(self, profile, state):
        state["updated_at"] = time.time()
        atomic_json(self._path(profile), state)

    def _lock(self, profile):
        return file_lock(self.root / (self._key(profile) + ".lock"))

    @staticmethod
    def _configured_revision(profile):
        return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()

    def effective_profile(self, configured_profile):
        """Return an independent profile with the last successfully selected LLVM.

        Selection is host state, never task input. Any change to the complete
        configured profile invalidates it, including recipes and permissions.
        Missing or obsolete state returns the configured profile unchanged;
        malformed current state fails closed instead of selecting an environment.
        """
        selected = copy.deepcopy(configured_profile)
        with self._lock(configured_profile):
            saved = read_json(self.root / (self._key(configured_profile) + ".selection.json"))
        if saved is None:
            return selected
        if not isinstance(saved, dict):
            raise WorkerError("invalid persisted environment selection")
        if saved.get("configured_revision") != self._configured_revision(configured_profile):
            return selected
        revision = saved.get("llvm_revision")
        if (saved.get("schema") != "triton-anchor-local-ci-environment-selection"
                or saved.get("profile_id") != configured_profile["id"]
                or not isinstance(revision, str)
                or (not re.fullmatch(r"[0-9a-f]{40}", revision)
                    and revision != configured_profile.get("llvm_revision"))
                or not isinstance(saved.get("llvm_selection"), dict)):
            raise WorkerError("invalid persisted environment selection")
        selected["llvm_revision"] = revision
        selected["llvm_selection"] = copy.deepcopy(saved["llvm_selection"])
        return selected

    def save_selection(self, configured_profile, selected_profile):
        """Persist a successfully prepared LLVM selection; return None.

        Call only after prepare succeeds. Container identity, recipes, tools and
        all other configured fields must be unchanged. Persistence is atomic and
        bound to the full configured profile, not just its starting LLVM hash.
        """
        configured = copy.deepcopy(configured_profile)
        selected = copy.deepcopy(selected_profile)
        revision = selected.pop("llvm_revision", None)
        metadata = selected.pop("llvm_selection", {})
        configured.pop("llvm_revision", None)
        configured.pop("llvm_selection", None)
        if (configured != selected or not isinstance(metadata, dict)
                or not isinstance(revision, str)
                or (not re.fullmatch(r"[0-9a-f]{40}", revision)
                    and revision != configured_profile.get("llvm_revision"))):
            raise WorkerError("selection may only change the LLVM revision and selection evidence")
        with self._lock(configured_profile):
            atomic_json(self.root / (self._key(configured_profile) + ".selection.json"), {
                "schema": "triton-anchor-local-ci-environment-selection",
                "profile_id": configured_profile["id"],
                "configured_revision": self._configured_revision(configured_profile),
                "llvm_revision": revision, "llvm_selection": metadata,
                "selected_at": time.time(),
            })

    def _docker(self, *args, check=True, timeout=120):
        result = self.run([self.docker, *map(str, args)], capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check and result.returncode:
            # Never echo Docker run arguments: trusted config may carry secrets.
            raise WorkerError(f"docker {args[0]} failed (exit {result.returncode})")
        return result

    def _container(self, name):
        result = self._docker("inspect", "--type", "container", name, check=False)
        if result.returncode:
            # Distinguish a missing worker from an unavailable daemon.
            self._docker("info", "--format", "{{.ServerVersion}}")
            return None
        return json.loads(result.stdout)[0]

    def _owned(self, profile, info):
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("org.triton-anchor.local-ci.profile") != profile["id"]:
            raise WorkerError("container name belongs to an unmanaged worker; refusing mutation")

    def _health(self, profile):
        command = profile["container"]["healthcheck"]
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise WorkerError("healthcheck must be a nonempty argv list")
        self._docker("exec", profile["container"]["name"], *command,
                     timeout=int(profile["container"].get("health_timeout_seconds", 120)))
        revision = profile.get("llvm_revision", "")
        if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
            # Independently verify immutable installation evidence; a Docker
            # label and a successful profile command do not prove the LLVM used.
            self._docker("exec", "--user", "0:0", profile["container"]["name"],
                         "/usr/bin/python3", "-I", "-c",
                         "import pathlib,sys; p=pathlib.Path('/opt/llvm/anchor-ci-llvm-revision'); "
                         "sys.exit(0 if p.is_file() and not p.is_symlink() "
                         "and p.stat().st_uid == 0 and not p.stat().st_mode & 0o022 "
                         "and p.read_text(encoding='utf-8').strip() == sys.argv[1] else 1)",
                         revision, timeout=30)

    def _create(self, profile, image=None):
        spec = profile["container"]
        args = spec.get("run_args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise WorkerError("run_args must be an argv list")
        if any(a == "--rm" or a.startswith("--rm=") or a == "--name" or a.startswith("--name=") for a in args):
            raise WorkerError("task-scoped --rm and name overrides are forbidden")
        self._docker("run", "-d", "--name", spec["name"], "--restart", "unless-stopped",
                     *args, "--label", "org.triton-anchor.local-ci.profile=" + profile["id"],
                     "--label", "org.triton-anchor.local-ci.llvm=" + profile.get("llvm_revision", ""),
                     image or spec["image"], *spec.get("command", ["sleep", "infinity"]))

    def _ensure_locked(self, profile, state):
        if state.get("transaction"):
            raise WorkerError("interrupted maintenance needs rebuild recovery")
        info = self._container(profile["container"]["name"])
        if info:
            self._owned(profile, info)
            if (info["Config"].get("Labels") or {}).get("org.triton-anchor.local-ci.llvm", "") != profile.get("llvm_revision", ""):
                raise WorkerError("LLVM profile changed; rebuild the trusted environment before acquiring tasks")
            if not info.get("State", {}).get("Running"):
                self._docker("start", profile["container"]["name"])
        else:
            self._create(profile)
        self._health(profile)
        return {"status": "ready", "profile_id": profile["id"], "container": profile["container"]["name"]}

    def ensure(self, profile):
        """Start/validate the fixed worker, returning ready identity; no task clone."""
        with self._lock(profile):
            state = self._state(profile)
            if state.get("draining"):
                raise WorkerBusy("worker is draining for maintenance")
            return self._ensure_locked(profile, state)

    def prepare(self, profile):
        """Return ready after ensure or a trusted rebuild for a changed LLVM.

        A live lease, drain or competing build raises WorkerBusy. A revision
        change without a trusted recipe or a failed build raises WorkerError.
        A missing worker is ensured from its configured image; the caller must
        build any newly selected image before this first creation when required.
        """
        with self._lock(profile):
            state = self._state(profile)
            if state.get("lease") or state.get("draining"):
                raise WorkerBusy("worker is draining or leased")
            info = self._container(profile["container"]["name"])
            if info:
                self._owned(profile, info)
            changed = bool(info and (info.get("Config", {}).get("Labels") or {}).get(
                "org.triton-anchor.local-ci.llvm", "") != profile.get("llvm_revision", ""))
            if not changed and not state.get("transaction"):
                return self._ensure_locked(profile, state)
            if not profile.get("maintenance", {}).get("recipe"):
                raise WorkerError("LLVM changed but no trusted maintenance recipe is configured")
        # rebuild takes the same profile lock and rechecks the lease. Never call
        # it while holding that lock; another task racing here only defers us.
        result = self.rebuild(profile, force=True)
        if result.get("status") == "waiting":
            raise WorkerBusy(result.get("reason", "worker is waiting for maintenance"))
        if result.get("status") != "ready":
            raise WorkerError(result.get("error", "trusted environment rebuild failed"))
        return {"profile_id": profile["id"], **result}

    def acquire(self, profile, task_id):
        """Atomically validate and lease the fixed worker; return lease dict.

        Only one task may own a profile. The lease does not expire automatically.
        Call release in finally AFTER the entire task process group has stopped.
        """
        if not isinstance(task_id, str) or not task_id:
            raise WorkerError("task_id is required")
        with self._lock(profile):
            state = self._state(profile)
            if state.get("draining") or state.get("lease"):
                raise WorkerBusy("worker is draining or leased")
            self._ensure_locked(profile, state)
            lease = {"task_id": task_id, "pid": os.getpid(), "acquired_at": time.time(),
                     "container": profile["container"]["name"]}
            state["lease"] = lease
            self._save(profile, state)
            return lease

    def release(self, profile, task_id):
        """Release this task's lease; reject attempts to release another task."""
        with self._lock(profile):
            state = self._state(profile)
            if state.get("lease") and state["lease"]["task_id"] != task_id:
                raise WorkerError("task does not own this worker")
            state["lease"] = None
            self._save(profile, state)

    def inspect(self, profile):
        """Read persisted lease/maintenance state plus Docker state; no mutation."""
        state = self._state(profile)
        info = self._container(profile["container"]["name"])
        state["container"] = profile["container"]["name"]
        state["running"] = bool(info and info.get("State", {}).get("Running"))
        if info:
            self._owned(profile, info)
            state["oom_killed"] = bool(info.get("State", {}).get("OOMKilled"))
        return state

    def _rollback(self, profile, state):
        transaction = state.get("transaction") or {}
        backup = transaction.get("backup")
        name = profile["container"]["name"]
        backup_info = self._container(backup) if backup else None
        if backup_info:
            self._owned(profile, backup_info)
            current = self._container(name)
            if current:
                self._owned(profile, current)
                self._docker("rm", "-f", name)
            self._docker("rename", backup, name)
            self._docker("start", name)
        elif transaction.get("had_old"):
            # Crash before rename: the original canonical container still exists.
            old = self._container(name)
            if not old:
                raise WorkerError("rollback worker is missing; manual recovery required")
            self._owned(profile, old)
            self._docker("start", name)
        else:
            current = self._container(name)
            if current:
                self._owned(profile, current)
                self._docker("rm", "-f", name)
        state.pop("transaction", None)
        state["draining"] = False
        self._save(profile, state)

    def rebuild(self, profile, force=False):
        """Drain, build trusted recipe, replace fixed worker, healthcheck/rollback.

        Return status ready/waiting/outside_window/already_current/failed.
        force bypasses the daily window only, NEVER a live lease or disk checks.
        Failed transactions remain recoverable from their host-side journal.
        """
        with self._lock(profile):
            state = self._state(profile)
            settings = profile.get("maintenance", {})
            if state.get("lease"):
                if force or window_open(settings):
                    state["draining"] = True
                    self._save(profile, state)
                return {"status": "waiting", "task_id": state["lease"]["task_id"]}
            if state.get("transaction"):
                self._rollback(profile, state)
            if not force and not window_open(settings):
                state["draining"] = False
                self._save(profile, state)
                return {"status": "outside_window"}
            today = datetime.now(timezone.utc).date().isoformat()
            if not force and state.get("rebuilt_on") == today and state.get("llvm_revision") == profile.get("llvm_revision", ""):
                state["draining"] = False
                self._save(profile, state)
                return {"status": "already_current"}
            state["draining"] = True
            self._save(profile, state)
            try:
                # Serialize builds across versions in addition to UTC staggering.
                with file_lock(self.root / "build.lock"):
                    recipe = settings.get("recipe")
                    if not recipe:
                        raise WorkerError("trusted maintenance recipe is not configured")
                    disk_path = settings.get("disk_path", str(self.root))
                    free = shutil.disk_usage(disk_path).free
                    if free < float(settings.get("min_free_gb", 20)) * 1024**3:
                        raise WorkerError("insufficient disk space for environment rebuild")
                    context = Path(recipe["context"]).resolve(strict=True)
                    dockerfile = (context / recipe.get("dockerfile", "Dockerfile")).resolve(strict=True)
                    if not dockerfile.is_relative_to(context):
                        raise WorkerError("Dockerfile must be inside the trusted recipe context")
                    build_args = dict(recipe.get("build_args", {}))
                    build_args["LLVM_REVISION"] = profile.get("llvm_revision", "")
                    args = ["build", "--pull", "--no-cache", "-t", profile["container"]["image"], "-f", str(dockerfile)]
                    for key, value in sorted(build_args.items()):
                        args.extend(["--build-arg", f"{key}={value}"])
                    self._docker(*args, str(context), timeout=int(settings.get("build_timeout_seconds", 14400)))
                    name = profile["container"]["name"]
                    old = self._container(name)
                    if old:
                        self._owned(profile, old)
                    backup = name + "-maintenance-rollback"
                    if self._container(backup):
                        raise WorkerError("rollback slot already exists; preserve it and investigate")
                    state["transaction"] = {"backup": backup, "had_old": bool(old), "started_at": time.time()}
                    self._save(profile, state)
                    if old:
                        self._docker("stop", "--time", "60", name)
                        self._docker("rename", name, backup)
                    self._create(profile)
                    self._health(profile)
                    # Save success before deleting rollback slot so a crash cannot
                    # accidentally treat the new worker as an unverified original.
                    state["transaction"]["verified"] = True
                    self._save(profile, state)
                    if old:
                        self._docker("rm", backup)
                    state.pop("transaction", None)
                    state.update(draining=False, rebuilt_on=today, llvm_revision=profile.get("llvm_revision", ""), last_error=None)
                    self._save(profile, state)
                    return {"status": "ready", "container": name, "rebuilt_on": today}
            except WorkerBusy as exc:
                state["draining"] = False
                self._save(profile, state)
                return {"status": "waiting", "reason": str(exc)}
            except (WorkerError, OSError, subprocess.SubprocessError) as exc:
                state["last_error"] = str(exc)
                if state.get("transaction"):
                    try:
                        self._rollback(profile, state)
                    except (WorkerError, OSError, subprocess.SubprocessError) as rollback_error:
                        state["last_error"] += f"; rollback failed: {rollback_error}"
                else:
                    state["draining"] = False
                self._save(profile, state)
                return {"status": "failed", "error": state["last_error"], "recoverable": True}
