"""Trusted Docker executor. The model cannot choose host commands or mounts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from .protocol import ContractError, atomic_json, within
from .control import validate_control_revision
from .policy import TOOLS

ALLOWED_PARAMETERS = {"max_jobs", "timeout_seconds", "operators", "kernels"}
PERFORMANCE_TOOLS = {"compile_time", "pass_profile", "ir_serialization"}
SECRET_NAMES = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CODEX", "CREDENTIAL", "GIT_ASKPASS", "SSH_AUTH_SOCK")

# Every descendant inherits an execution marker, including tools which start
# another session. Cancellation never trusts a candidate-writable pid file.
STOP_PROGRAM = r'''
import os,signal,sys,time
marker=('LOCAL_CI_EXECUTION_ID='+sys.argv[1]).encode()
def matching():
    found=[]
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name)==os.getpid(): continue
        try:
            if marker in open('/proc/'+name+'/environ','rb').read().split(b'\0'):
                found.append(int(name))
        except (OSError,PermissionError): pass
    return found
for sig in (signal.SIGTERM,signal.SIGKILL):
    for pid in matching():
        try: os.kill(pid,sig)
        except ProcessLookupError: pass
    time.sleep(.2)
'''

VENV_PROGRAM = r'''
import json,os,pathlib,subprocess,sys,sysconfig
target=pathlib.Path(sys.argv[1]); fingerprint=sys.argv[2]
marker=target/'.local-ci-environment.json'
if marker.exists():
    if json.loads(marker.read_text()).get('fingerprint') != fingerprint:
        raise RuntimeError('Task venv belongs to another environment generation')
    if not (target/'bin/python').is_file(): raise RuntimeError('Task venv is incomplete')
    sys.exit(0)
if target.exists(): raise RuntimeError('Incomplete task venv requires explicit recovery')
subprocess.run([sys.executable,'-m','venv','--copies',str(target)],check=True)
try:
    destination=json.loads(subprocess.check_output([str(target/'bin/python'),'-c','import json,sysconfig; print(json.dumps(sysconfig.get_paths()))']))
    copied=set()
    for key in ('purelib','platlib'):
        source=pathlib.Path(sysconfig.get_path(key)); dest=pathlib.Path(destination[key])
        if (str(source),str(dest)) in copied: continue
        copied.add((str(source),str(dest)))
        # Clone data where supported; never link mutable package files between variants.
        subprocess.run(['cp','-aL','--reflink=auto',str(source)+ '/.',str(dest)],check=True)
    marker.write_text(json.dumps({'fingerprint':fingerprint,'seed_python':sys.executable}))
except BaseException:
    # A later run must not silently accept partially installed dependencies.
    raise
'''


@contextlib.contextmanager
def resource_lock(state_dir: Path, cancelled: threading.Event):
    import fcntl
    with (state_dir / "resource.lock").open("a") as stream:
        while True:
            if cancelled.is_set():
                raise InterruptedError("Task cancelled while waiting for resources")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                cancelled.wait(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class DockerExecutor:
    def __init__(self, config: dict, state_dir: Path, generation: dict, task: dict,
                 relay, *, command_runner=None):
        self.config, self.state_dir, self.generation, self.task = config, Path(state_dir), generation, task
        self.relay = relay
        if not re.fullmatch(r"[a-f0-9]{64}", task["task_id"]):
            raise ContractError("Invalid task identity")
        self.host_root = Path(generation["workspace_host"]) / "tasks" / task["task_id"]
        self.container_root = Path(generation["workspace_container"]) / "tasks" / task["task_id"]
        self.host_root.mkdir(parents=True, exist_ok=True)
        for path in (self.host_root.parent, self.host_root):
            if path.is_symlink():
                raise ContractError("Task parent must not be a symlink")
            path.chmod(0o711)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runner = command_runner or subprocess.run
        self.processes: dict[str, subprocess.Popen] = {}
        self.guard = threading.Lock()
        self.prepare_guard = threading.RLock()
        self.execution_user = str(generation.get("execution_user") or config.get("container_execution_user") or "")
        if not self.execution_user or self.execution_user.split(":")[0] in {"0", "root"}:
            raise ContractError("A dedicated non-root container execution_user is required")
        self.uid = generation.get("execution_uid")
        self.gid = generation.get("execution_gid")
        if self.uid is None and self.execution_user.split(":")[0].isdigit():
            self.uid = int(self.execution_user.split(":")[0])
            self.gid = int(self.execution_user.split(":")[1]) if ":" in self.execution_user else None
        if type(self.uid) is not int or type(self.gid) is not int or self.uid <= 0 or self.gid < 0:
            raise ContractError("Generation must include resolved execution_uid and execution_gid")
        self.baseline_root = self.state_dir / "baselines" / task["task_id"]
        self.record_root = self.state_dir / "executor-records" / task["task_id"]

    def writable(self, path: Path, *, recursive: bool = False) -> None:
        if not path.resolve().is_relative_to(self.host_root.resolve()) or path.is_symlink():
            raise ContractError("Task writable path escaped its root")
        paths = [path, *path.rglob("*")] if recursive else [path]
        for child in paths:
            if child.is_symlink():
                continue
            if os.geteuid() == 0:
                os.chown(child, self.uid, self.gid)
            elif os.geteuid() != self.uid:
                raise ContractError("Worker cannot assign task workspace ownership")

    def mapped_host_path(self, container_path: str) -> Path:
        path = Path(container_path)
        prefix = Path(self.generation["workspace_container"])
        if not path.is_absolute() or not path.is_relative_to(prefix) or path == prefix:
            raise ContractError("Mutable backend checkout must be inside the mapped profile workspace")
        return within(Path(self.generation["workspace_host"]), str(path.relative_to(prefix)))

    def prepare(self, variant: str = "candidate") -> Path:
        if variant not in {"candidate", "base"}:
            raise ContractError("Unknown checkout variant")
        with self.prepare_guard:
            directory = self.host_root / variant
            directory.mkdir(exist_ok=True)
            checkout = directory / "checkout"
            self.relay.checkout(self.task["tested_sha" if variant == "candidate" else "base_sha"], checkout)
            self.writable(directory)
            self.writable(checkout, recursive=True)
            if self.generation.get("backend_enabled"):
                backend = directory / "backend"
                source = self.mapped_host_path(self.generation.get("env", {}).get("BACKEND_PATH", ""))
                if not backend.exists():
                    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-hardlinks", "--", str(source), str(backend)], check=True, capture_output=True)
                self.writable(backend, recursive=True)
            return checkout

    def environment(self, execution_id: str, variant: str, parameters: dict) -> dict[str, str]:
        if set(parameters) - ALLOWED_PARAMETERS:
            raise ContractError("Unsupported tool parameters")
        jobs = parameters.get("max_jobs", self.config.get("max_jobs", 8))
        if type(jobs) is not int or not 1 <= jobs <= self.config.get("max_jobs", 8):
            raise ContractError("Parallelism exceeds the trusted budget")
        trusted = {str(k): str(v) for k, v in self.generation.get("env", {}).items()}
        if any(any(word in k.upper() for word in SECRET_NAMES) for k in trusted):
            raise ContractError("Credentials must not enter the candidate container")
        variant_root = self.container_root / variant
        artifact = self.container_root / "artifacts" / execution_id
        trusted.update({
            "WORKSPACE": self.generation["workspace_container"],
            "ANCHOR_DIR": str(variant_root / "checkout"),
            "LOCAL_CI_TASK_ROOT": str(variant_root),
            "LOCAL_CI_ARTIFACT_DIR": str(artifact),
            "LOCAL_CI_TOOL_RESULT": str(artifact / "result.json"),
            "LOCAL_CI_TASK_ID": self.task["task_id"],
            "LOCAL_CI_EXECUTION_ID": execution_id,
            "LOCAL_CI_BASE_SHA": self.task["base_sha"],
            "LOCAL_CI_PROFILE_NAME": str(self.generation["profile"]),
            "LOCAL_CI_LLVM_HASH": self.task["llvm_hash"],
            "LOCAL_CI_TESTED_SHA": self.task["tested_sha" if variant == "candidate" else "base_sha"],
            "LOCAL_CI_ENVIRONMENT_FINGERPRINT": self.generation["environment_fingerprint"],
            "MAX_JOBS": str(jobs), "CMAKE_BUILD_PARALLEL_LEVEL": str(jobs),
            "NINJAFLAGS": f"-j{jobs}", "FRONTEND_BUILD_MODE": "fresh",
            "FLAGGEMS_RANDOM_SEED": str(int(hashlib.sha256((self.task['task_id'] + self.task['tested_sha'] + 'impact/v4').encode()).hexdigest()[:8], 16)),
            "FLAGGEMS_TEST_MODE": "full" if self.task["full"] else "sample",
            "PYTHON_VENV_ACTIVATE": str(variant_root / "venv/bin/activate"),
            "PYTHON_BIN": str(variant_root / "venv/bin/python"),
            "PATH": str(variant_root / "venv/bin") + ":" + trusted.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(variant_root / "home"), "LANG": "C.UTF-8",
            "RUN_BACKEND_STAGES": "true" if self.generation["backend_enabled"] else "false",
        })
        if self.generation.get("backend_enabled"):
            trusted["BACKEND_PATH"] = str(variant_root / "backend")
        if variant == "base":
            llvm_path = self.host_root / "base/checkout/triton/cmake/llvm-hash.txt"
            if llvm_path.is_file() and llvm_path.read_text().strip() != self.task["llvm_hash"]:
                raise ContractError("Base uses a different LLVM environment; same-environment baseline unavailable")
        for parameter, names in (("operators", ("FLAGGEMS_AFFECTED_OPS",)), ("kernels", ("COMPILE_BENCHMARK_KERNELS", "PASS_PROFILE_KERNELS", "IR_SERIALIZATION_KERNELS"))):
            if parameter in parameters:
                values = parameters[parameter]
                if not isinstance(values, list) or not values or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_]+", v) for v in values):
                    raise ContractError(f"Invalid {parameter}")
                trusted.update({name: ",".join(values) for name in names})
        return trusted

    def docker_prefix(self, env: dict | None = None) -> list[str]:
        args = [self.config.get("docker_bin", "docker"), "exec"]
        args += ["--user", self.execution_user]
        # Docker image ENV is not a trust boundary: clear it before the child starts.
        clean = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **(env or {})}
        values = []
        for key, value in clean.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ContractError("Invalid trusted environment variable")
            values.append(f"{key}={value}")
        return args + [self.generation["container"], "env", "-i", *values]

    def stop(self, execution_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
            raise ContractError("Invalid execution id")
        try:
            self.runner(self.docker_prefix() + [self.config.get("container_python", "python3"), "-c", STOP_PROGRAM, execution_id], capture_output=True, timeout=15)
        finally:
            self._stop_client(execution_id)

    def _stop_client(self, execution_id: str) -> None:
        with self.guard:
            process = self.processes.get(execution_id)
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def set_baseline(self, tool_id: str, execution_id: str) -> dict:
        if tool_id not in PERFORMANCE_TOOLS or not re.fullmatch(r"[a-f0-9]{32}", execution_id):
            raise ContractError("Invalid baseline tool/execution")
        artifact = self.host_root / "artifacts" / execution_id
        record = json.loads((self.record_root / (execution_id + ".json")).read_text())
        source = within(artifact, "candidate.json", must_exist=True)
        if record.get("tool_id") != tool_id or record.get("variant") != "base" or record.get("status") != "pass" or record.get("environment_fingerprint") != self.generation["environment_fingerprint"]:
            raise ContractError("Baseline must be a successful base execution in this environment")
        if record.get("candidate_digest") != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ContractError("Baseline artifact changed after its execution")
        payload = json.loads(source.read_text())
        metadata = payload.get("metadata", {})
        if metadata.get("commit_sha") != self.task["base_sha"] or metadata.get("environment_fingerprint") != self.generation["environment_fingerprint"]:
            raise ContractError("Baseline candidate identity mismatch")
        sealed = self.baseline_root / (tool_id + ".json")
        atomic_json(sealed, payload)
        entry = {"status": "available", "tool_id": tool_id, "execution_id": execution_id,
                 "digest": hashlib.sha256(sealed.read_bytes()).hexdigest(), "environment_fingerprint": self.generation["environment_fingerprint"]}
        atomic_json(self.baseline_root / (tool_id + "-record.json"), entry)
        return entry

    def get_baseline(self, tool_id: str) -> dict:
        if tool_id not in PERFORMANCE_TOOLS:
            raise ContractError("Unknown performance tool")
        entry = self.baseline_root / (tool_id + "-record.json")
        payload = self.baseline_root / (tool_id + ".json")
        if not entry.is_file() or not payload.is_file():
            unavailable = self.baseline_root / (tool_id + "-unavailable.json")
            if unavailable.is_file():
                return {**json.loads(unavailable.read_text()), "tool_id": tool_id}
            return {"status": "unavailable", "reason": "base_check_not_executed", "tool_id": tool_id}
        value = json.loads(entry.read_text())
        if value.get("environment_fingerprint") != self.generation["environment_fingerprint"] or value.get("digest") != hashlib.sha256(payload.read_bytes()).hexdigest():
            return {"status": "unavailable", "reason": "base_artifact_stale_or_changed", "tool_id": tool_id}
        return value

    def seed_python(self) -> str:
        profile = self.generation.get("env", {})
        seed = profile.get("SEED_PYTHON")
        if not seed and profile.get("PYTHON_VENV_ACTIVATE"):
            seed = str(Path(profile["PYTHON_VENV_ACTIVATE"]).parent / "python")
        if not seed or not Path(seed).is_absolute():
            raise ContractError("Profile must identify an absolute seed Python or venv activation path")
        return seed

    def _execute(self, command: list[str], execution_id: str, log: Path,
                 cancelled: threading.Event, timeout: int) -> tuple[int, str]:
        """Bound both Docker client and independently re-sessioned container children."""
        if cancelled.is_set():
            raise InterruptedError("Task cancelled before execution")
        with log.open("ab") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            with self.guard:
                self.processes[execution_id] = process
            reason = ""
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancelled.wait(0.1):
                        reason = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        reason = "timeout"
                        break
                if reason:
                    self.stop(execution_id)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                else:
                    # A successful parent can still leave daemonized descendants.
                    # Remove them before releasing the shared hardware lock.
                    self.stop(execution_id)
                return process.returncode, reason
            finally:
                if process.poll() is None:
                    self.stop(execution_id)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                with self.guard:
                    self.processes.pop(execution_id, None)

    def run(self, tool_id: str, execution_id: str, variant: str, parameters: dict,
            cancelled: threading.Event, custom: dict | None = None) -> dict:
        if custom is not None and tool_id in (*TOOLS, "contract_tests"):
            raise ContractError("Custom execution cannot replace a built-in tool")
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id) or variant not in {"candidate", "base"}:
            raise ContractError("Invalid execution identity or variant")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,80}", tool_id):
            raise ContractError("Invalid tool identity")
        started = time.time()
        artifact_host = self.host_root / "artifacts" / execution_id
        artifact_host.parent.mkdir(exist_ok=True)
        artifact_host.parent.chmod(0o711)
        artifact_host.mkdir(parents=True, exist_ok=False)
        self.writable(artifact_host)
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "status": "infra_error", "started_at": started, "exit_code": None,
                  "environment_fingerprint": self.generation["environment_fingerprint"],
                  "artifact_dir": str(artifact_host), "parameters": parameters}
        try:
            with resource_lock(self.state_dir, cancelled):
                record["worker_revision_sha"] = validate_control_revision(self.config, self.task)
                checkout = self.prepare(variant)
                env = self.environment(execution_id, variant, parameters)
                limit = self.config.get("tool_timeouts", {}).get(tool_id, 3600)
                timeout = parameters.get("timeout_seconds", limit)
                if type(timeout) is not int or not 1 <= timeout <= limit:
                    raise ContractError("Invalid tool deadline")
                env["LOCAL_CI_TOOL_TIMEOUT_SECONDS"] = str(timeout)
                home = self.host_root / variant / "home"
                home.mkdir(exist_ok=True)
                self.writable(home)
                venv = self.container_root / variant / "venv"
                seed_env = {str(k): str(v) for k, v in self.generation.get("env", {}).items()}
                seed_env.update({"LOCAL_CI_EXECUTION_ID": execution_id, "HOME": env["HOME"]})
                code, reason = self._execute(self.docker_prefix(seed_env) + [self.seed_python(), "-c", VENV_PROGRAM,
                    str(venv), self.generation["environment_fingerprint"]], execution_id,
                    artifact_host / "execution.log", cancelled, self.config.get("venv_timeout_seconds", 600))
                if reason == "cancelled":
                    raise InterruptedError("Task cancelled preparing Python environment")
                if code or reason:
                    raise RuntimeError("Could not prepare task Python environment: " + (reason or str(code)))
                if tool_id in PERFORMANCE_TOOLS and variant == "candidate":
                    baseline = self.get_baseline(tool_id)
                    record["baseline"] = baseline
                    if baseline["status"] == "available":
                        # Only a host-sealed base result can become a comparison input.
                        relative = Path(".trusted/baselines") / (tool_id + ".json")
                        sealed = self.host_root / relative
                        atomic_json(sealed, json.loads((self.baseline_root / (tool_id + ".json")).read_text()))
                        sealed.parent.parent.chmod(0o711)
                        sealed.parent.chmod(0o711)
                        sealed.chmod(0o444)
                        env["BASELINE_JSON"] = str(self.container_root / relative)
                executable = ["bash", self.config.get("container_control_root", "/opt/local-ci/control") + "/scripts/local_ci/tools/run_tool.sh", tool_id]
                if custom:
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", custom["name"]):
                        raise ContractError("Invalid custom script name")
                    if custom["language"] not in {"python", "bash"}:
                        raise ContractError("Unsupported custom language")
                    source_only = custom.get("source_only", False)
                    if type(source_only) is not bool or source_only and custom["language"] != "python":
                        raise ContractError("Source-only checks must use Python")
                    path = within(self.host_root / variant, "generated/" + custom["name"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(custom["content"], encoding="utf-8")
                    self.writable(path.parent)
                    self.writable(path)
                    container_path = self.container_root / variant / path.relative_to(self.host_root / variant)
                    executable = ([env["PYTHON_BIN"], "-I", *(["-S"] if source_only else [])] if custom["language"] == "python" else ["bash"]) + [str(container_path)]
                    record.update(script_digest=hashlib.sha256(custom["content"].encode()).hexdigest(),
                                  script_name=custom["name"], source_sha=self.task["tested_sha" if variant == "candidate" else "base_sha"], source_only=source_only)
                    (artifact_host / custom["name"]).write_text(custom["content"], encoding="utf-8")
                launch = 'cd "$ANCHOR_DIR" || exit 2; exec "$@"'
                command = self.docker_prefix(env) + ["bash", "-c", launch, "--", *executable]
                record["command"] = executable
                record["cwd"] = str(self.container_root / variant / "checkout")
                code, reason = self._execute(command, execution_id, artifact_host / "execution.log", cancelled, timeout)
                record["exit_code"] = code
                record["status"] = "cancelled" if reason == "cancelled" else "infra_error" if reason else "pass" if code == 0 else "fail"
                record["reason"] = reason or ("completed" if code == 0 else "command_failed")
                with (artifact_host / "execution.log").open("rb") as log:
                    log.seek(max(0, log.seek(0, 2) - 1024 * 1024))
                    log_text = log.read().decode(errors="replace")
                if not reason and (code in {137, -9} or re.search(r"out of memory|oom.kill|killed signal terminated", log_text, re.I)):
                    record.update(status="infra_error", reason="oom")
                trusted_git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=" + str(checkout)]
                dirty = subprocess.check_output([*trusted_git, "status", "--porcelain", "--untracked-files=no"], cwd=checkout)
                actual = subprocess.check_output([*trusted_git, "rev-parse", "HEAD"], cwd=checkout).decode().strip()
                if dirty or actual != self.task["tested_sha" if variant == "candidate" else "base_sha"]:
                    record.update(status="infra_error", reason="frozen_checkout_modified")
                detail = artifact_host / "result.json"
                if detail.is_file() and not detail.is_symlink():
                    if detail.stat().st_size > 16 * 1024 * 1024:
                        raise ContractError("Tool result is too large")
                    record["details"] = json.loads(detail.read_text())
                    if not isinstance(record["details"], dict):
                        raise ContractError("Tool result must be an object")
                    if not custom and (record["details"].get("tool_id") != tool_id or record["details"].get("tested_sha") != env["LOCAL_CI_TESTED_SHA"] or record["details"].get("environment_fingerprint") != env["LOCAL_CI_ENVIRONMENT_FINGERPRINT"]):
                        raise ContractError("Tool result identity mismatch")
                    if record["status"] == "pass" and record["details"].get("status") != "pass":
                        record.update(status="fail", reason="tool_reported_failure")
                elif not custom and record["status"] == "pass":
                    record.update(status="infra_error", reason="tool_result_missing")
                if tool_id in PERFORMANCE_TOOLS and record["status"] == "pass":
                    candidate = within(artifact_host, "candidate.json", must_exist=True)
                    if candidate.stat().st_size > 16 * 1024 * 1024:
                        raise ContractError("Performance result is too large")
                    record["candidate_digest"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except InterruptedError as exc:
            record.update(status="cancelled", reason=str(exc))
        except Exception as exc:
            record.update(status="infra_error", reason=str(exc))
        record["duration_seconds"] = round(time.time() - started, 3)
        atomic_json(self.record_root / (execution_id + ".json"), record)
        atomic_json(artifact_host / "executor-record.json", record)
        if tool_id in PERFORMANCE_TOOLS and variant == "base":
            if record["status"] == "pass":
                try:
                    record["baseline"] = self.set_baseline(tool_id, execution_id)
                except Exception as exc:
                    record.update(status="infra_error", reason="Could not seal baseline: " + str(exc))
            else:
                record["baseline"] = {"status": "unavailable", "reason": record.get("reason", "base_check_failed")}
                # Preserve an auditable reason when no valid base sample exists.
                if self.get_baseline(tool_id)["status"] != "available":
                    atomic_json(self.baseline_root / (tool_id + "-unavailable.json"), record["baseline"])
            atomic_json(self.record_root / (execution_id + ".json"), record)
            atomic_json(artifact_host / "executor-record.json", record)
        return record
