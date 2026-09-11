"""One execution service for the A tool plans and task-local commands."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from .control import validate_control_revision
from .protocol import ContractError, atomic_json
from tools.basic_tools.runner import environment_command, plan
from tools.basic_tools.evidence import evaluate

PERFORMANCE_TOOLS = {"compile_time", "pass_profile", "ir_serialization"}
SECRET_NAMES = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "API_KEY",
    "CODEX",
    "CREDENTIAL",
    "GIT_ASKPASS",
    "SSH_AUTH_SOCK",
)

# Each invocation owns one process group. Cancelling it never sweeps the shared UID.
LAUNCH_PROGRAM = r"""
import json,os,pathlib,signal,sys,time
child=os.fork()
if child==0:
    try:
        os.setsid()
        root=pathlib.Path('/task/.processes'); root.mkdir(exist_ok=True)
        pid=os.getpid()
        start=pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
        path=root/(os.environ['LOCAL_CI_EXECUTION_ID']+'.json')
        tmp=root/(path.name+'.tmp.'+str(pid))
        tmp.write_text(json.dumps({'pid':pid,'start':start,'started_at':time.time()}))
        os.replace(tmp,path)
        os.execvpe(sys.argv[1],sys.argv[1:],os.environ)
    except BaseException as exc:
        print('Local CI launcher failed: '+str(exc),file=sys.stderr)
        os._exit(125)
def forward(signum,frame):
    try: os.killpg(child,signum)
    except ProcessLookupError: pass
for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
    signal.signal(signum,forward)
while True:
    try:
        _,status=os.waitpid(child,0)
        break
    except InterruptedError:
        pass
if os.WIFEXITED(status): sys.exit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status): sys.exit(128+os.WTERMSIG(status))
sys.exit(125)
"""
STOP_PROGRAM = r"""
import json,os,pathlib,signal,sys,time
p=pathlib.Path('/task/.processes')/(sys.argv[1]+'.json')
if not p.exists(): print(json.dumps({'verified':True,'remaining':[]}));sys.exit()
r=json.loads(p.read_text()); pid=r['pid']
try:
    start=pathlib.Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
    if start!=r['start']: raise RuntimeError('Process identity changed')
except FileNotFoundError:
    p.unlink(missing_ok=True);print(json.dumps({'verified':True,'remaining':[]}));sys.exit()
for sig in (signal.SIGTERM,signal.SIGKILL):
    try: os.killpg(pid,sig)
    except ProcessLookupError: break
    time.sleep(.1)
p.unlink(missing_ok=True)
print(json.dumps({'verified':True,'remaining':[]}))
"""
CODEX_LAUNCH_PROGRAM = r"""
import json,os,sys
with open('/task/session/environment.json') as f: env=json.load(f)
os.chdir(env['LOCAL_CI_CODEX_WORKSPACE'])
os.environ.clear();os.environ.update(env)
os.environ['LOCAL_CI_EXECUTION_ID']='codex'
program=sys.argv[1];sys.argv=sys.argv[1:];exec(program)
"""


@contextlib.contextmanager
def resource_lock(state_dir, cancelled):
    import fcntl

    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with (Path(state_dir) / "resource.lock").open("a") as stream:
        while True:
            if cancelled.is_set():
                raise InterruptedError("Task cancelled waiting for resources")
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
    pass


class DockerExecutor:
    def __init__(
        self,
        config,
        state_dir,
        generation,
        task,
        relay,
        *,
        command_runner=None,
        manager=None,
    ):
        self.config, self.state_dir, self.generation, self.task = (
            config,
            Path(state_dir),
            generation,
            task,
        )
        self.relay, self.manager = relay, manager
        self.runner = command_runner or subprocess.run
        self.run_dir = self.state_dir / "runs" / task["task_id"] / generation["run_id"]
        self.host_root = (
            self.state_dir / "work" / task["task_id"] / generation["run_id"]
        )
        self.container_root = Path("/task")
        self.uid = generation.get(
            "execution_uid", generation.get("uids", {}).get("task")
        )
        self.gid = generation.get(
            "execution_gid", generation.get("gids", {}).get("task")
        )
        if any(type(x) is not int or x <= 0 for x in (self.uid, self.gid)):
            raise ContractError("A task requires one non-root execution user")
        self.execution_user = f"{self.uid}:{self.gid}"
        self.processes = {}
        self.guard = threading.RLock()
        self.prepare_guard = threading.RLock()
        self.cancelled_ids = set()
        self.records = {}
        self.baselines = {}
        self.journal = None
        for name in ("logs", "artifacts", "inputs"):
            (self.run_dir / name).mkdir(parents=True, exist_ok=True)

    def docker_command(self, *args):
        from ops_maint.manager import docker_command

        return docker_command(self.config, *args)

    def docker_prefix(self, env=None, *, management=False, role=None):
        values = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            **(env or {}),
        }
        return self.docker_command(
            "exec",
            "--user",
            "0:0" if management else self.execution_user,
            self.generation["container_id"],
            "env",
            "-i",
            *[f"{k}={v}" for k, v in values.items()],
        )

    def prepare(self, variant="candidate"):
        if variant not in {"candidate", "base"}:
            raise ContractError("Unknown source version")
        with self.prepare_guard:
            root = self.run_dir / "inputs" / variant
            checkout = root / "checkout"
            root.mkdir(parents=True, exist_ok=True)
            sha = self.task["base_sha" if variant == "base" else "tested_sha"]
            self.relay.checkout(sha, checkout)
            self.relay.checkout_submodules(self.task, sha, checkout)
            marker = root / "imported.json"
            if not marker.exists():
                archive = root / "checkout.tar"
                with tarfile.open(archive, "w") as stream:
                    for child in sorted(checkout.iterdir()):
                        stream.add(child, arcname=child.name)
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                self.manager.import_checkout(self.generation, variant, archive, digest)
                atomic_json(marker, {"sha256": digest, "tested_sha": sha})
                archive.unlink()
            return checkout

    def environment(self, execution_id, variant, parameters):
        root = self.container_root / variant
        env = {str(k): str(v) for k, v in self.generation.get("env", {}).items()}
        if any(any(word in k.upper() for word in SECRET_NAMES) for k in env):
            raise ContractError("Profile environment contains service credentials")
        jobs = parameters.get("jobs", self.config.get("max_jobs", 8))
        if type(jobs) is not int or not 1 <= jobs <= self.config.get("max_jobs", 8):
            raise ContractError("Build parallelism exceeds task budget")
        env.update(
            ANCHOR_DIR=str(root / "checkout"),
            LOCAL_CI_TASK_ROOT=str(root),
            WORKSPACE="/task",
            LOCAL_CI_ARTIFACT_DIR=f"/task/artifacts/{execution_id}",
            LOCAL_CI_EXECUTION_ID=execution_id,
            LOCAL_CI_TASK_ID=self.task["task_id"],
            LOCAL_CI_TESTED_SHA=self.task[
                "base_sha" if variant == "base" else "tested_sha"
            ],
            LOCAL_CI_ENVIRONMENT_FINGERPRINT=self.generation["environment_fingerprint"],
            PYTHON_BIN=str(root / "venv/bin/python"),
            PYTHON_VENV_ACTIVATE=str(root / "venv/bin/activate"),
            PATH=str(root / "venv/bin")
            + ":"
            + env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            HOME=str(root / "home"),
            TMPDIR=str(root / "tmp"),
            XDG_CACHE_HOME=str(root / "cache"),
            TRITON_CACHE_DIR=str(root / "cache/triton"),
            MAX_JOBS=str(jobs),
            CMAKE_BUILD_PARALLEL_LEVEL=str(jobs),
            LANG="C.UTF-8",
        )
        if self.generation.get("backend_enabled"):
            env["BACKEND_PATH"] = str(root / "backend")
        return env

    def plan_context(self, execution_id, variant, parameters):
        env = self.environment(execution_id, variant, parameters)
        root = self.container_root / variant
        original = self.generation.get("env", {})
        profile_config = self.config.get("profiles", {}).get(
            self.generation.get("profile_branch", self.task.get("target_branch")), {}
        )
        tools = dict(profile_config.get("tools", {}))
        scripts = []
        if original.get("TRUSTED_ANCHOR_ENVSETUP"):
            scripts.append({"path": original["TRUSTED_ANCHOR_ENVSETUP"], "args": []})
        backend_scripts = []
        if original.get("BACKEND_ENVSETUP"):
            path = Path(original["BACKEND_ENVSETUP"])
            if not path.is_absolute():
                path = root / "backend" / path
            backend_scripts.append(
                {
                    "path": str(path),
                    "args": shlex.split(original.get("BACKEND_ENVSETUP_ARGS", "")),
                }
            )
        tools.update(
            python_bin=env["PYTHON_BIN"],
            llvm_dir=original.get("LLVM_BUILD_DIR"),
            backend_dir=str(root / "backend"),
            expected_backend=original.get(
                "EXPECTED_TRITON_BACKEND", tools.get("expected_backend")
            ),
            flaggems_dir=original.get("FLAGGEMS_CLONE_DIR", tools.get("flaggems_dir")),
            env=env,
            env_scripts=scripts,
            backend_env_scripts=backend_scripts,
        )
        if original.get("BACKEND_TEST_COMMAND") and "backend_smoke_argv" not in tools:
            tools["backend_smoke_argv"] = [
                "bash",
                "-c",
                original["BACKEND_TEST_COMMAND"],
            ]
        tools.setdefault(
            "backend_test_paths",
            shlex.split(original.get("BACKEND_TEST_PATHS", "tests")),
        )
        tools.setdefault(
            "backend_wheel_pattern", original.get("BACKEND_WHEEL_PATTERN", "*.whl")
        )
        dependencies = {}
        expected_imports = {}
        if self.journal:
            for record in self.journal.executions(self.task["task_id"]):
                if record.get("variant") == variant and record["status"] == "pass":
                    if record.get("artifact_dir"):
                        relative = Path(record["artifact_dir"]).relative_to(
                            self.run_dir / "artifacts"
                        )
                        dependencies[record["tool_id"]] = (
                            "/task/artifacts/" + relative.as_posix()
                        )
                    if record["tool_id"] == "frontend_install":
                        expected_imports = (
                            record.get("details", {})
                            .get("installation", {})
                            .get("imports", {})
                        )
        return {
            "source_dir": str(root / "checkout"),
            "artifact_dir": f"/task/artifacts/{execution_id}",
            "task_root": str(root),
            "task_id": self.task["task_id"],
            "target_sha": env["LOCAL_CI_TESTED_SHA"],
            "base_sha": self.task["base_sha"],
            "triton_version": str(
                self.generation.get("triton_version")
                or original.get("LOCAL_CI_TRITON_VERSION")
                or profile_config.get("triton_version")
                or self.generation["profile"]
            ).replace("triton-", ""),
            "backend_enabled": self.generation["backend_enabled"],
            "python_bin": env["PYTHON_BIN"],
            "trusted_python_bin": self.config.get(
                "container_python", "/usr/bin/python3"
            ),
            "tools_dir": self.config.get(
                "container_control_root", "/opt/local-ci/control"
            )
            + "/scripts/local_ci/tools",
            "task_venv": str(root / "venv"),
            "environment_fingerprint": self.generation["environment_fingerprint"],
            "control_revision": self.task["worker_revision_sha"],
            "dependency_artifacts": dependencies,
            "expected_imports": expected_imports,
            "profile": {
                "id": self.generation["profile"],
                "llvm_revision": self.task["llvm_hash"],
                "backend_enabled": self.generation["backend_enabled"],
                "tools": tools,
            },
            "performance_baselines": dict(self.baselines),
        }

    def seed_python(self):
        env = self.generation.get("env", {})
        seed = env.get("SEED_PYTHON")
        if not seed and env.get("PYTHON_VENV_ACTIVATE"):
            seed = str(Path(env["PYTHON_VENV_ACTIVATE"]).parent / "python")
        if not seed or not Path(seed).is_absolute():
            raise ContractError("Profile must identify an absolute seed Python")
        return seed

    def _execute(self, argv, cwd, env, ident, log, cancelled, timeout):
        if cancelled.is_set() or ident in self.cancelled_ids:
            return None, "cancelled"
        command = self.docker_prefix(env) + [
            self.config.get("container_python", "python3"),
            "-I",
            "-S",
            "-c",
            LAUNCH_PROGRAM,
            "bash",
            "-c",
            'cd "$1" && shift && exec "$@"',
            "--",
            cwd,
            *argv,
        ]
        with log.open("ab") as stream:
            process = subprocess.Popen(
                command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
            )
            with self.guard:
                self.processes[ident] = process
            deadline = time.monotonic() + timeout
            reason = ""
            try:
                while process.poll() is None:
                    if cancelled.wait(0.1) or ident in self.cancelled_ids:
                        reason = "cancelled"
                        break
                    if time.monotonic() > deadline:
                        reason = "timeout"
                        break
                if reason:
                    self._stop_group(ident)
                if process.poll() is None:
                    process.wait(timeout=10)
                # Group cleanup also catches children surviving their launching command.
                self._stop_group(ident)
                return process.returncode, reason
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                with self.guard:
                    self.processes.pop(ident, None)

    def _stop_group(self, ident):
        report = self.runner(
            self.docker_prefix()
            + [
                self.config.get("container_python", "python3"),
                "-I",
                "-S",
                "-c",
                STOP_PROGRAM,
                ident,
            ],
            capture_output=True,
            timeout=self.config.get("cleanup_timeout_seconds", 60),
        )
        if report.returncode:
            raise ProcessCleanupError("Task command process group cleanup failed")
        return json.loads(report.stdout)

    def stop(self, execution_id):
        self.cancelled_ids.add(execution_id)
        return self._stop_group(execution_id)

    def stop_task(self, task_id=None):
        if task_id and task_id != self.task["task_id"]:
            raise ContractError("Wrong task identity")
        with self.guard:
            identifiers = list(self.processes)
        for ident in identifiers:
            self.stop(ident)
        return {"verified": True, "remaining": []}

    def stop_codex(self):
        return self._stop_group("codex")

    def prepare_codex_session(self, *, files, environment, rpc_socket):
        if files or environment:
            self.manager.deploy_session(self.generation, files, environment)
        return {
            "home": "/task/session/home",
            "workspace": "/task/candidate/checkout",
            "python_bin": self.config.get("container_python", "/usr/bin/python3"),
            "mcp_script": self.config.get(
                "container_control_root", "/opt/local-ci/control"
            )
            + "/scripts/local_ci/agent_ci/mcp_server.py",
            "rpc_socket": "/task/rpc/" + Path(rpc_socket).name,
        }

    def codex_command(self, arguments):
        binary = self.config.get("codex_bin", "")
        if not Path(binary).is_absolute():
            raise ContractError("codex_bin must be an absolute container path")
        return self.docker_command(
            "exec",
            "--interactive",
            "--user",
            self.execution_user,
            self.generation["container_id"],
            self.config.get("container_python", "/usr/bin/python3"),
            "-I",
            "-S",
            "-c",
            CODEX_LAUNCH_PROGRAM,
            LAUNCH_PROGRAM,
            binary,
            *arguments,
        )

    def prepare_native_workspace(self):
        self.prepare()
        layout = self.manager.prepare_native_workspace(self.generation)
        setup = [[str(Path(layout["root"]) / "venv/bin/activate")]]
        env = self.generation.get("env", {})
        if env.get("TRUSTED_ANCHOR_ENVSETUP"):
            setup.append([env["TRUSTED_ANCHOR_ENVSETUP"]])
        if env.get("BACKEND_ENVSETUP"):
            path = Path(env["BACKEND_ENVSETUP"])
            setup.append(
                [
                    str(
                        path
                        if path.is_absolute()
                        else Path(layout["root"]) / "backend" / path
                    ),
                    *shlex.split(env.get("BACKEND_ENVSETUP_ARGS", "")),
                ]
            )
        return {
            **layout,
            "artifact_root": "/task/artifacts",
            "tools_root": self.config.get(
                "container_control_root", "/opt/local-ci/control"
            )
            + "/scripts/local_ci/tools",
            "environment_setup": setup,
            "setup_automatic": False,
        }

    def native_environment(self, layout):
        return self.environment("native", "candidate", {})

    def export_native_evidence(self, destination):
        return self.manager.export_native_evidence(self.generation, destination)

    def diagnostic_context(self):
        return self.manager.runtime_info(self.generation)

    def source_identity(self, variant):
        sha = self.task["base_sha" if variant == "base" else "tested_sha"]
        identity = self.manager.verify_checkout(self.generation, variant, sha)
        return {
            "tested_sha": sha,
            "original": bool(identity.get("verified")),
            "source": identity,
        }

    def current_identity(self, variant):
        identity = self.source_identity(variant)
        digest = hashlib.sha256()
        # Hash installed distribution records and their files, never LLVM/SDK caches.
        venv = self.host_root / variant / "venv"
        import csv

        for record in sorted(venv.glob("lib/python*/site-packages/*.dist-info/RECORD")):
            package = record.parent.name.lower()
            if not any(name in package for name in ("triton", "anchor")):
                continue
            digest.update(record.relative_to(venv).as_posix().encode())
            digest.update(record.read_bytes())
            for row in csv.reader(record.read_text().splitlines()):
                file = (record.parent.parent / row[0]).resolve()
                if not file.is_relative_to(venv.resolve()):
                    continue
                if file.is_file():
                    info = file.stat()
                    digest.update(row[0].encode())
                    digest.update(
                        f"{info.st_size}:{info.st_mtime_ns}:{info.st_ino}".encode()
                    )
        identity["installation_identity"] = digest.hexdigest()
        return identity

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        started = time.time()
        root = self.run_dir / "artifacts" / execution_id
        log = self.run_dir / "logs" / (execution_id + ".log")
        record = {
            "execution_id": execution_id,
            "tool_id": tool_id,
            "variant": variant,
            "started_at": started,
            "status": "infra_error",
            "exit_code": None,
            "parameters": parameters,
            "artifact_dir": str(root / tool_id),
            "log_path": str(log),
            "environment_fingerprint": self.generation["environment_fingerprint"],
            "workspace_generation": self.generation["generation"],
            "tested_sha": self.task["base_sha" if variant == "base" else "tested_sha"],
            "command": [],
            "cwd": f"/task/{variant}/checkout",
        }
        try:
            with resource_lock(self.state_dir, cancelled):
                validate_control_revision(self.config, self.task)
                self.prepare(variant)
                self.manager.prepare_execution(
                    self.generation, execution_id, variant, diagnostic=bool(custom)
                )
                env = self.environment(execution_id, variant, parameters)
                context = self.plan_context(execution_id, variant, parameters)
                subject_before = self.source_identity(variant)
                record["subject_before"] = subject_before
                # One environment preparer serves Agent, native commands and builtin plans.
                self.manager.prepare_workspace(self.generation, variant)
                if custom:
                    mode = custom.get("mode", "diagnostic")
                    if mode == "experiment":
                        exp = custom.get("experiment_id") or execution_id
                        self.manager.create_experiment(self.generation, exp, variant)
                        context["source_dir"] = f"/task/experiments/{exp}/checkout"
                        env.update(
                            ANCHOR_DIR=context["source_dir"],
                            PYTHON_BIN=f"/task/experiments/{exp}/venv/bin/python",
                        )
                        record["experiment_id"] = exp
                    self.manager.write_execution_file(
                        self.generation, execution_id, custom["name"], custom["content"]
                    )
                    path = f"/task/.trusted/scripts/{execution_id}/{custom['name']}"
                    argv = (
                        [
                            env["PYTHON_BIN"],
                            *(["-I", "-S"] if custom.get("source_only") else []),
                        ]
                        if custom["language"] == "python"
                        else ["bash"]
                    ) + [path]
                    argv = environment_command(
                        argv,
                        context["profile"]["tools"],
                        context["tools_dir"],
                        backend=self.generation["backend_enabled"],
                    )
                    specification = {
                        "commands": [
                            {
                                "argv": argv,
                                "cwd": context["source_dir"],
                                "env": env,
                                "timeout": self.config.get(
                                    "custom_timeout_seconds", 3600
                                ),
                            }
                        ]
                    }
                    root.mkdir(parents=True, exist_ok=True)
                    (root / custom["name"]).write_text(custom["content"])
                    record.update(
                        artifact_dir=str(root),
                        script_name=custom["name"],
                        script_digest=hashlib.sha256(
                            custom["content"].encode()
                        ).hexdigest(),
                        custom_mode=mode,
                        script_language=custom["language"],
                        execution_kind="custom",
                    )
                else:
                    specification = plan(tool_id, context, parameters)
                    if specification["status"] != "ready":
                        raise ContractError(
                            specification.get("reason", "Unsupported tool capability")
                        )
                observed = []
                for item in specification["commands"]:
                    begin = time.time()
                    limit = min(
                        item["timeout"],
                        self.config.get("tool_timeouts", {}).get(
                            tool_id, item["timeout"]
                        ),
                    )
                    code, reason = self._execute(
                        item["argv"],
                        item["cwd"],
                        {**env, **item["env"]},
                        execution_id,
                        log,
                        cancelled,
                        limit,
                    )
                    observed.append(
                        {
                            "argv": item["argv"],
                            "cwd": item["cwd"],
                            "started_at": begin,
                            "finished_at": time.time(),
                            "exit_code": code,
                        }
                    )
                    if code or reason:
                        break
                record.update(
                    command=observed,
                    exit_code=code,
                    reason=reason or ("completed" if code == 0 else "command_failed"),
                )
                observed_subject = self.source_identity(variant)
                record["subject"] = observed_subject
                self.manager.export_execution(self.generation, execution_id, root)
                record["evidence_exported"] = True
                status = (
                    "cancelled"
                    if reason == "cancelled"
                    else "infra_error"
                    if reason
                    else "pass"
                    if code == 0
                    else "fail"
                )
                if not custom:
                    outcome = evaluate(
                        tool_id, root / tool_id, code, context, parameters
                    )
                    record.update(outcome)
                    record["artifact_subject"] = outcome.get("subject", {})
                    record["subject"] = observed_subject
                    if reason:
                        record["status"] = status
                else:
                    record["status"] = status
                record["installation_identity"] = self.current_identity(variant)[
                    "installation_identity"
                ]
                record["original_subject"] = (
                    subject_before["original"]
                    and record["subject"].get("original", False)
                    and not record.get("experiment_id")
                )
                if not record["original_subject"]:
                    record["attribution"] = "modified_candidate"
                if code in (137, -9):
                    record.update(status="infra_error", reason="oom")
                if (
                    tool_id in PERFORMANCE_TOOLS
                    and variant == "base"
                    and record["status"] == "pass"
                ):
                    self.baselines[tool_id] = {
                        "path": f"/task/artifacts/{execution_id}/{tool_id}/candidate.json",
                        "sha256": hashlib.sha256(
                            (root / tool_id / "candidate.json").read_bytes()
                        ).hexdigest(),
                        "base_sha": self.task["base_sha"],
                        "profile_id": self.generation["profile"],
                        "llvm_revision": self.task["llvm_hash"],
                        "environment_fingerprint": self.generation[
                            "environment_fingerprint"
                        ],
                    }
        except InterruptedError as exc:
            record.update(status="cancelled", reason=str(exc))
        except Exception as exc:
            record.update(status="infra_error", reason=str(exc))
        record.update(
            finished_at=time.time(), duration_seconds=round(time.time() - started, 3)
        )
        self.records[execution_id] = record
        return record
