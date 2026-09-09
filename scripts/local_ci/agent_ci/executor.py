"""Trusted Docker executor. The model cannot choose host commands or mounts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from .protocol import ContractError, atomic_json, within
from .control import validate_control_revision
from .policy import TOOLS

ALLOWED_PARAMETERS = {"max_jobs", "timeout_seconds", "operators", "kernels"}
PERFORMANCE_TOOLS = {"compile_time", "pass_profile", "ir_serialization"}
SECRET_NAMES = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CODEX", "CREDENTIAL", "GIT_ASKPASS", "SSH_AUTH_SOCK")

# Each task has separate Codex, candidate, base and diagnostic UIDs. The worker
# holds the shared resource lock whenever it launches or cleans an execution UID.
# Markers remain useful evidence, but process ownership is the cleanup boundary.
STOP_PROGRAM = r'''
import json,os,signal,sys,time
uid=int(sys.argv[1]); signalled=set(); zombies=set()
def identity(pid):
    with open('/proc/'+str(pid)+'/status') as stream:
        fields=dict(line.rstrip().split(':',1) for line in stream if ':' in line)
    return [int(value) for value in fields['Uid'].split()],fields['State'].strip()
def scan():
    found=[]; protected=[]
    for name in os.listdir('/proc'):
        if not name.isdigit(): continue
        pid=int(name)
        try:
            ids,state=identity(pid)
            if uid not in ids: continue
            if pid in (1,os.getpid()) or 0 in ids:
                protected.append(pid); continue
            if state.startswith('Z'):
                zombies.add(pid); continue
            found.append(pid)
        except (FileNotFoundError,ProcessLookupError): pass
    if protected: raise RuntimeError('Dedicated CI UID overlaps protected PID/root identity: '+str(protected))
    return found
def send(pid,sig):
    try:
        descriptor=os.pidfd_open(pid,0)
        try:
            # Recheck after acquiring a PID-stable handle. PID reuse cannot
            # redirect this signal to an unrelated process.
            ids,state=identity(pid)
            if uid in ids and 0 not in ids and pid not in (1,os.getpid()) and not state.startswith('Z'):
                signal.pidfd_send_signal(descriptor,sig,None,0)
                signalled.add(pid)
        finally: os.close(descriptor)
    except (FileNotFoundError,ProcessLookupError): pass
report={'schema':'triton-anchor-process-cleanup/v1','uid':uid,'remaining':[],'zombies':[],'cleaned_pid_count':0}
try:
    if uid <= 0 or os.geteuid()!=0: raise RuntimeError('Cleanup requires a non-root target UID and trusted root reaper')
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise RuntimeError('PID-stable cleanup requires Linux pidfd support')
    descriptor=os.pidfd_open(os.getpid(),0)
    try: signal.pidfd_send_signal(descriptor,0,None,0)
    finally: os.close(descriptor)
    for sig,seconds in ((signal.SIGTERM,.5),(signal.SIGKILL,4.0)):
        deadline=time.monotonic()+seconds
        while True:
            remaining=scan()
            if not remaining: break
            for pid in remaining: send(pid,sig)
            if time.monotonic()>=deadline: break
            time.sleep(.05)
    # Two scans after signals also catch children forked during termination.
    remaining=scan()
    if not remaining:
        time.sleep(.05)
        remaining=scan()
    report.update(remaining=remaining,zombies=sorted(zombies),cleaned_pid_count=len(signalled),
                  status='clean' if not remaining else 'failed',verified=True)
    if remaining: report['error']='CI UID still has live processes after SIGKILL'
except Exception as exc:
    report.update(status='failed',verified=False,error=str(exc),cleaned_pid_count=len(signalled))
print(json.dumps(report))
sys.exit(0 if report.get('status')=='clean' else 1)
'''

# Irreversibly prevent exec-time setuid/file-capability privilege gains before
# any seed or candidate command runs, so descendants retain the CI UID boundary.
LAUNCH_PROGRAM = r'''
import ctypes,os,sys
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(38,1,0,0,0)!=0:
    raise OSError(ctypes.get_errno(),'Could not set PR_SET_NO_NEW_PRIVS')
os.execvpe(sys.argv[1],sys.argv[1:],os.environ)
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

CODEX_LAUNCH_PROGRAM = r'''
import ctypes,json,os,sys
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(38,1,0,0,0)!=0: raise OSError(ctypes.get_errno(),'no_new_privs')
with open('/codex/environment.json') as stream: environment=json.load(stream)
if not isinstance(environment,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in environment.items()):
    raise RuntimeError('Invalid private Codex environment')
os.chdir('/codex/workspace')
os.execvpe(sys.argv[1],sys.argv[1:],environment)
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


class ProcessCleanupError(RuntimeError):
    """Infrastructure failure: process cleanup was not proven complete."""


CUSTOM_ENV_PROGRAM = r"""
import json,os,pathlib,subprocess,sys
original=dict(os.environ); current=dict(original)
protected={"HOME","TMPDIR","TRITON_CACHE_DIR","TRITON_DUMP_DIR","XDG_CACHE_HOME","ANCHOR_DIR","BACKEND_PATH","PYTHON_BIN","PYTHON_VENV_ACTIVATE","MAX_JOBS","CMAKE_BUILD_PARALLEL_LEVEL","NINJAFLAGS","BASELINE_JSON"}
protected.update(k for k in original if k.startswith("LOCAL_CI_"))
for index,command in enumerate(json.loads(original["LOCAL_CI_CUSTOM_ENVSETUP"])):
    log=pathlib.Path(original["LOCAL_CI_ARTIFACT_DIR"])/("setup-%02d.log" % index)
    with log.open("wb") as stream:
        result=subprocess.run(["bash","--noprofile","--norc","-c",'set -e; source "$1" "${@:2}" >&2; env -0',"setup",*command],env=current,stdout=subprocess.PIPE,stderr=stream,timeout=60)
    if result.returncode:
        print("Environment setup failed; see "+log.name,file=sys.stderr);sys.exit(78)
    current=dict(item.decode().split("=",1) for item in result.stdout.split(b"\0") if item)
    current.update({k:original[k] for k in protected if k in original})
current["PATH"]=str(pathlib.Path(original["PYTHON_BIN"]).parent)+":"+current.get("PATH","/usr/local/bin:/usr/bin:/bin")
current["VIRTUAL_ENV"]=str(pathlib.Path(original["PYTHON_BIN"]).parent.parent)
anchor=pathlib.Path(original["ANCHOR_DIR"])
current["PYTHONPATH"]=":".join(p for p in current.get("PYTHONPATH","").split(":") if p and pathlib.Path(p).is_absolute() and not pathlib.Path(p).resolve().is_relative_to(anchor))
os.execvpe(sys.argv[1],sys.argv[1:],current)
"""


class DockerExecutor:
    def __init__(self, config: dict, state_dir: Path, generation: dict, task: dict,
                 relay, *, command_runner=None, manager=None):
        self.config, self.state_dir, self.generation, self.task = config, Path(state_dir), generation, task
        self.relay = relay
        if not re.fullmatch(r"[a-f0-9]{64}", task["task_id"]):
            raise ContractError("Invalid task identity")
        if generation.get("task_id") != task["task_id"]:
            raise ContractError("Container handle belongs to another task")
        attempt = generation.get("attempt_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", attempt):
            raise ContractError("Executor requires a registered task container attempt")
        self.host_root = self.state_dir / "task-staging" / task["task_id"] / attempt
        self.container_root = Path("/task")
        self.host_root.mkdir(parents=True, exist_ok=True)
        for path in (self.host_root.parent, self.host_root):
            if path.is_symlink():
                raise ContractError("Task parent must not be a symlink")
            path.chmod(0o711)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runner = command_runner or subprocess.run
        if manager is None:
            from environments.manager import EnvironmentManager
            manager = EnvironmentManager(config, state_dir)
        self.manager = manager
        self.processes: dict[str, subprocess.Popen] = {}
        self.process_roles: dict[str, str] = {}
        self.guard = threading.Lock()
        self.prepare_guard = threading.RLock()
        self.cleanup_guard = threading.RLock()
        roles = {"candidate", "base", "diagnostic", "codex"}
        self.uids, self.gids = generation.get("uids", {}), generation.get("gids", {})
        if (set(self.uids) != roles or set(self.gids) != roles or len(set(self.uids.values())) != 4
                or any(type(n) is not int or n <= 0 for n in [*self.uids.values(), *self.gids.values()])
                or self.gids["codex"] in {self.gids[k] for k in roles - {"codex"}}):
            raise ContractError("Task requires four distinct non-root identities and a private Codex group")
        self.uid, self.gid = self.uids["candidate"], self.gids["candidate"]
        self.execution_user = f"{self.uid}:{self.gid}"
        self.baseline_root = self.state_dir / "baselines" / task["task_id"]
        self.record_root = self.state_dir / "executor-records" / task["task_id"]

    def prepare(self, variant: str = "candidate") -> Path:
        if variant not in {"candidate", "base"}:
            raise ContractError("Unknown checkout variant")
        with self.prepare_guard:
            directory = self.host_root / variant
            directory.mkdir(exist_ok=True)
            checkout = directory / "checkout"
            self.relay.checkout(self.task["tested_sha" if variant == "candidate" else "base_sha"], checkout)
            marker = directory / "imported.json"
            if not marker.exists():
                archive = directory / "checkout.tar"
                # The host copy is trusted frozen input, never a writable mount.
                with tarfile.open(archive, "w") as stream:
                    for child in sorted(checkout.iterdir()):
                        stream.add(child, arcname=child.name, recursive=True)
                value = hashlib.sha256()
                with archive.open("rb") as data:
                    for block in iter(lambda: data.read(1024 * 1024), b""):
                        value.update(block)
                digest = value.hexdigest()
                self.manager.import_checkout(self.generation, variant, archive, digest)
                atomic_json(marker, {"attempt_id": self.generation["attempt_id"], "sha256": digest})
                archive.unlink()
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
            "WORKSPACE": str(self.container_root),
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
            "TMPDIR": str(variant_root / "tmp"),
            "TRITON_CACHE_DIR": str(variant_root / "cache/triton"),
            "XDG_CACHE_HOME": str(variant_root / "cache"),
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

    def docker_command(self, *arguments: str) -> list[str]:
        from environments.manager import docker_command
        return docker_command(self.config, *arguments)

    def docker_prefix(self, env: dict | None = None, *, management: bool = False,
                      role: str = "candidate") -> list[str]:
        if role not in self.uids:
            raise ContractError("Unknown execution role")
        args = self.docker_command("exec")
        args += ["--user", "0:0" if management else f"{self.uids[role]}:{self.gids[role]}"]
        # Docker image ENV is not a trust boundary: clear it before the child starts.
        clean = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **(env or {})}
        values = []
        for key, value in clean.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ContractError("Invalid trusted environment variable")
            values.append(f"{key}={value}")
        prefix = args + [self.generation["container_id"], "env", "-i", *values]
        if not management:
            prefix += [self.config.get("container_python", "python3"), "-I", "-S", "-c", LAUNCH_PROGRAM]
        return prefix

    def prepare_codex_session(self, *, files: dict, environment: dict, rpc_socket: Path) -> dict:
        expected = Path(self.generation["rpc_host_dir"])
        if Path(rpc_socket).parent.resolve() != expected.resolve():
            raise ContractError("Codex socket does not belong to this attempt")
        if set(files) - {"config.toml", "auth.json", "TASK_SKILL.md"}:
            raise ContractError("Unknown Codex session file")
        if files or environment:
            self.manager.deploy_session(self.generation, files, environment)
        root = self.config.get("container_control_root", "/opt/local-ci/control")
        return {"home": "/codex/home", "workspace": "/codex/workspace",
                "python_bin": self.config.get("container_python", "/usr/bin/python3"),
                "mcp_script": root + "/scripts/local_ci/agent_ci/mcp_server.py",
                "rpc_socket": self.generation.get("rpc_container_dir", "/run/local-ci-rpc") + "/" + Path(rpc_socket).name}

    def codex_command(self, arguments: list[str]) -> list[str]:
        binary = self.config.get("codex_bin", "")
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            raise ContractError("codex_bin must be an absolute path inside the trusted image")
        return self.docker_command("exec", "--interactive", "--user",
            f"{self.uids['codex']}:{self.gids['codex']}", self.generation["container_id"],
            self.config.get("container_python", "/usr/bin/python3"), "-I", "-S", "-c",
            CODEX_LAUNCH_PROGRAM, binary, *arguments)

    def stop_codex(self) -> dict:
        return self.cleanup_processes(role="codex")

    def diagnostic_context(self) -> dict:
        return self.manager.runtime_info(self.generation)

    def cleanup_processes(self, task_id: str | None = None, *, role: str = "candidate") -> dict:
        """Verify no live CI-UID processes remain; caller holds resource.lock.

        The UID is exclusive to serial CI work. PID 1 and root management
        identities are never signalled. Defunct zombies are reported separately.
        """
        if task_id is not None and task_id != self.task["task_id"]:
            raise ContractError("Process cleanup is restricted to this task")
        if role not in self.uids:
            raise ContractError("Invalid process role")
        uid = self.uids[role]
        timeout = self.config.get("cleanup_timeout_seconds", 60)
        if type(timeout) is not int or timeout <= 0:
            raise ProcessCleanupError("cleanup_timeout_seconds must be a positive integer")
        with self.cleanup_guard:
            try:
                completed = self.runner(self.docker_prefix(management=True) + [
                    self.config.get("container_python", "python3"), "-I", "-S", "-c", STOP_PROGRAM, str(uid)],
                    capture_output=True, timeout=timeout)
                report = json.loads(completed.stdout)
                if (completed.returncode != 0 or not isinstance(report, dict)
                        or report.get("schema") != "triton-anchor-process-cleanup/v1" or report.get("uid") != uid
                        or report.get("status") != "clean" or report.get("verified") is not True
                        or report.get("remaining") != [] or type(report.get("cleaned_pid_count")) is not int):
                    detail = report.get("error", "cleanup did not verify an empty live-process set") if isinstance(report, dict) else "invalid cleanup report"
                    raise ProcessCleanupError("Container process cleanup failed: " + str(detail))
                return {**report, "task_id": self.task["task_id"], "role": role}
            except ProcessCleanupError:
                raise
            except Exception as exc:
                raise ProcessCleanupError("Container process cleanup could not be verified: " + str(exc)) from exc

    def stop_task(self, task_id: str | None = None) -> dict:
        """Reap a task after close/restart, including processes absent from memory."""
        if task_id is not None and task_id != self.task["task_id"]:
            raise ContractError("Process cleanup is restricted to this task")
        try:
            reports = [self.cleanup_processes(task_id, role=role) for role in ("candidate", "base", "diagnostic")]
            return {"verified": True, "remaining": [], "status": "clean", "roles": reports,
                    "cleaned_pid_count": sum(r["cleaned_pid_count"] for r in reports)}
        finally:
            with self.guard:
                executions = list(self.processes)
            for execution_id in executions:
                self._stop_client(execution_id)

    def stop(self, execution_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", execution_id):
            raise ContractError("Invalid execution id")
        with self.cleanup_guard:
            with self.guard:
                # A queued record may refer to work which has not acquired the
                # resource lock. It cannot authorize killing another UID owner.
                if execution_id not in self.processes:
                    return {"status": "not_active", "verified": False, "remaining": [], "cleaned_pid_count": 0}
            try:
                return self.cleanup_processes(role=self.process_roles[execution_id])
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
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise ProcessCleanupError("Docker client did not exit after SIGKILL") from exc

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
                 cancelled: threading.Event, timeout: int, *, role: str = "candidate") -> tuple[int, str]:
        """Bound both Docker client and independently re-sessioned container children."""
        if cancelled.is_set():
            raise InterruptedError("Task cancelled before execution")
        with log.open("ab") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            with self.guard:
                self.processes[execution_id] = process
                self.process_roles[execution_id] = role
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
            finally:
                with self.cleanup_guard:
                    try:
                        # Also reap detached descendants after successful parent
                        # exit; any failure overrides success/cancellation as infra.
                        self.stop(execution_id)
                    finally:
                        with self.guard:
                            self.processes.pop(execution_id, None)
                            self.process_roles.pop(execution_id, None)
            return process.returncode, reason

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
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "status": "infra_error", "started_at": started, "exit_code": None,
                  "environment_fingerprint": self.generation["environment_fingerprint"],
                  "workspace_generation": self.generation["generation"],
                  "artifact_dir": str(artifact_host), "parameters": parameters,
                  "tested_sha": self.task["tested_sha" if variant == "candidate" else "base_sha"],
                  "attempt_id": self.generation["attempt_id"], "container_id": self.generation["container_id"],
                  "image_release_id": self.generation["image_release_id"]}
        prepared = False
        try:
            with resource_lock(self.state_dir, cancelled):
                record["worker_revision_sha"] = validate_control_revision(self.config, self.task)
                self.prepare(variant)
                env = self.environment(execution_id, variant, parameters)
                role = "diagnostic" if custom else variant
                mode = custom.get("mode", "diagnostic") if custom else None
                if custom and mode not in {"diagnostic", "reproduction", "experiment"}:
                    raise ContractError("Unknown custom execution mode")
                self.manager.prepare_execution(self.generation, execution_id, variant, diagnostic=bool(custom))
                prepared = True
                limit = self.config.get("tool_timeouts", {}).get(tool_id, 3600)
                timeout = parameters.get("timeout_seconds", limit)
                if type(timeout) is not int or not 1 <= timeout <= limit:
                    raise ContractError("Invalid tool deadline")
                env["LOCAL_CI_TOOL_TIMEOUT_SECONDS"] = str(timeout)
                venv = self.container_root / variant / "venv"
                seed_env = {str(k): str(v) for k, v in self.generation.get("env", {}).items()}
                seed_env.update({"LOCAL_CI_EXECUTION_ID": execution_id, "HOME": env["HOME"]})
                if not custom:
                    code, reason = self._execute(self.docker_prefix(seed_env, role=role) + [self.seed_python(), "-c", VENV_PROGRAM,
                        str(venv), self.generation["environment_fingerprint"]], execution_id,
                        artifact_host / "execution.log", cancelled, self.config.get("venv_timeout_seconds", 600), role=role)
                    if reason == "cancelled":
                        raise InterruptedError("Task cancelled preparing Python environment")
                    if code or reason:
                        raise RuntimeError("Could not prepare task Python environment: " + (reason or str(code)))
                if tool_id in PERFORMANCE_TOOLS and variant == "candidate":
                    baseline = self.get_baseline(tool_id)
                    record["baseline"] = baseline
                    if baseline["status"] == "available":
                        # Only a host-sealed base result can become a comparison input.
                        self.manager.write_baseline(self.generation, tool_id,
                            json.loads((self.baseline_root / (tool_id + ".json")).read_text()))
                        env["BASELINE_JSON"] = str(self.container_root / ".trusted/baselines" / (tool_id + ".json"))
                executable = ["bash", self.config.get("container_control_root", "/opt/local-ci/control") + "/scripts/local_ci/tools/run_tool.sh", tool_id]
                if custom:
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", custom["name"]):
                        raise ContractError("Invalid custom script name")
                    if custom["language"] not in {"python", "bash"}:
                        raise ContractError("Unsupported custom language")
                    source_only = custom.get("source_only", False)
                    if type(source_only) is not bool or source_only and custom["language"] != "python":
                        raise ContractError("Source-only checks must use Python")
                    self.manager.authorize_diagnostics(self.generation)
                    runtime = self.diagnostic_context().get(variant, {})
                    if mode == "reproduction" and not source_only and not runtime.get("python_available"):
                        raise ContractError("Reproduction requires the original installed variant environment")
                    scratch = self.container_root / "diagnostics" / execution_id
                    python = runtime.get("python_bin") if runtime.get("python_available") else self.seed_python()
                    if source_only:
                        python = self.seed_python()
                    env.update(HOME=str(scratch / "home"), TMPDIR=str(scratch / "tmp"),
                               XDG_CACHE_HOME=str(scratch / "cache"), TRITON_CACHE_DIR=str(scratch / "cache/triton"),
                               PYTHONDONTWRITEBYTECODE="1", PYTHON_BIN=python)
                    experiment_id = custom.get("experiment_id")
                    if mode == "experiment":
                        experiment_id = experiment_id or execution_id
                        if not re.fullmatch(r"[a-f0-9]{32}", experiment_id):
                            raise ContractError("Invalid experiment identity")
                        self.manager.create_experiment(self.generation, experiment_id, variant)
                        experiment = self.container_root / "experiments" / experiment_id
                        env.update(ANCHOR_DIR=str(experiment / "checkout"),
                                   LOCAL_CI_TASK_ROOT=str(experiment),
                                   PYTHON_BIN=str(experiment / "venv/bin/python"),
                                   PYTHON_VENV_ACTIVATE=str(experiment / "venv/bin/activate"))
                        if self.generation.get("backend_enabled"):
                            env["BACKEND_PATH"] = str(experiment / "backend")
                    elif experiment_id is not None:
                        raise ContractError("Only experiments accept an experiment_id")
                    env["PATH"] = str(Path(env["PYTHON_BIN"]).parent) + ":" + self.generation.get("env", {}).get("PATH", "/usr/local/bin:/usr/bin:/bin")
                    self.manager.write_execution_file(self.generation, execution_id, custom["name"], custom["content"])
                    container_path = self.container_root / ".trusted/scripts" / execution_id / custom["name"]
                    executable = ([env["PYTHON_BIN"], "-I", *(["-S"] if source_only else [])] if custom["language"] == "python" else ["bash"]) + [str(container_path)]
                    record.update(script_digest=hashlib.sha256(custom["content"].encode()).hexdigest(),
                                  script_name=custom["name"], source_sha=record["tested_sha"], source_only=source_only,
                                  custom_mode=mode, runtime_origin="experiment" if mode == "experiment" else
                                  "seed" if source_only or not runtime.get("python_available") else "variant",
                                  execution_uid=self.uids[role], experiment_id=experiment_id)
                    (artifact_host / custom["name"]).write_text(custom["content"], encoding="utf-8")
                launched = executable
                if custom and mode in {"reproduction", "experiment"} and not custom.get("source_only"):
                    setup = [[env["PYTHON_VENV_ACTIVATE"]]]
                    if env.get("TRUSTED_ANCHOR_ENVSETUP"):
                        setup.append([env["TRUSTED_ANCHOR_ENVSETUP"]])
                    if self.generation.get("backend_enabled") and env.get("BACKEND_ENVSETUP"):
                        path = Path(env["BACKEND_ENVSETUP"])
                        if not path.is_absolute():
                            path = Path(env["BACKEND_PATH"]) / path
                        setup.append([str(path), *shlex.split(env.get("BACKEND_ENVSETUP_ARGS", ""))])
                    env["LOCAL_CI_CUSTOM_ENVSETUP"] = json.dumps(setup)
                    record["environment_setup"] = setup
                    launched = [self.config.get("container_python", "python3"), "-I", "-S", "-c", CUSTOM_ENV_PROGRAM, *executable]
                launch = 'cd "$ANCHOR_DIR" || exit 2; exec "$@"'
                command = self.docker_prefix(env, role=role) + ["bash", "-c", launch, "--", *launched]
                record["command"] = executable
                record["cwd"] = env["ANCHOR_DIR"]
                record["execution_uid"] = self.uids[role]
                code, reason = self._execute(command, execution_id, artifact_host / "execution.log", cancelled, timeout, role=role)
                record["exit_code"] = code
                record["status"] = "cancelled" if reason == "cancelled" else "infra_error" if reason else "pass" if code == 0 else "fail"
                record["reason"] = reason or ("completed" if code == 0 else "command_failed")
                if custom and record.get("environment_setup") and code == 78:
                    record.update(status="infra_error", reason="custom_environment_setup_failed")
                with (artifact_host / "execution.log").open("rb") as log:
                    log.seek(max(0, log.seek(0, 2) - 1024 * 1024))
                    log_text = log.read().decode(errors="replace")
                if not reason and (code in {137, -9} or re.search(r"out of memory|oom.kill|killed signal terminated", log_text, re.I)):
                    record.update(status="infra_error", reason="oom")
                source = self.manager.verify_checkout(self.generation, variant, record["tested_sha"])
                if not source.get("verified") or source.get("dirty") or source.get("sha") != record["tested_sha"]:
                    record.update(status="infra_error", reason="frozen_checkout_modified")
                self.manager.export_execution(self.generation, execution_id, artifact_host)
                record["evidence_exported"] = True
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
        if prepared and not record.get("evidence_exported"):
            try:
                with resource_lock(self.state_dir, threading.Event()):
                    self.cleanup_processes(role="diagnostic" if custom else variant)
                    self.manager.export_execution(self.generation, execution_id, artifact_host)
                record["evidence_exported"] = True
            except Exception as exc:
                record.update(status="infra_error", evidence_export_error=str(exc))
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
