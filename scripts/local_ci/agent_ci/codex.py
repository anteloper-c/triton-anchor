"""Dedicated server-only Codex session with a task-scoped MCP capability."""
from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .protocol import ContractError, atomic_json
from .mcp_server import SCHEMAS
from .skill import load_skill

UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
DISABLED_FEATURES = {
    "shell_tool", "unified_exec", "apps", "hooks", "plugins", "browser_use",
    "computer_use", "multi_agent", "image_generation", "memories", "shell_snapshot",
    "skill_mcp_dependency_install", "code_mode_host", "js_repl", "python_repl",
    "apply_patch_freeform",
}
MODEL_SETTINGS = {
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "model_context_window", "model_auto_compact_token_limit",
}


def finish_timeout_seconds(config: dict) -> int:
    """Two host/container snapshots, device check, reaping and evidence storage."""
    snapshot = config.get("hygiene_snapshot_timeout_seconds", 600)
    cleanup = config.get("cleanup_timeout_seconds", 60)
    checks = [profile.get("post_task_validation_timeout_seconds", 120)
              for profile in config.get("profiles", {}).values()]
    if any(type(value) is not int or not 1 <= value <= 3600 for value in [snapshot, *checks]):
        raise ContractError("Invalid environment validation timeout")
    if type(cleanup) is not int or cleanup < 1:
        raise ContractError("Invalid process cleanup timeout")
    device_checks = [profile.get("post_task_validation_timeout_seconds", 120)
                     * max(1, len(profile.get("post_task_validation_commands", [])))
                     for profile in config.get("profiles", {}).values()]
    seconds = 4 * snapshot + max(device_checks, default=120) + 2 * cleanup + 600
    if seconds > 86300:
        raise ContractError("Combined finish deadline exceeds one day")
    return seconds


def load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            try:
                from codex_ai.validate_codex_ai_credentials import parse_toml_fallback
            except ImportError:
                from ..codex_ai.validate_codex_ai_credentials import parse_toml_fallback
            return parse_toml_fallback(path.read_text(encoding="utf-8"))
    return tomllib.loads(path.read_text(encoding="utf-8"))


def toml_value(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(k)) + " = " + toml_value(v) for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if type(value) in {str, int, float, bool}:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ContractError("Unsupported value in deployed Codex provider configuration")


def write_config(path: Path, settings: dict) -> None:
    path.write_text("\n".join(json.dumps(key) + " = " + toml_value(value) for key, value in settings.items()) + "\n", encoding="utf-8")
    path.chmod(0o600)


class CodexDriver:
    def __init__(self, config: dict, state_dir: Path):
        self.config, self.state_dir = config, Path(state_dir)

    @staticmethod
    def account(user: str):
        if not user:
            raise ContractError("codex_user must be a dedicated account without Docker/journal write access")
        account = pwd.getpwnam(user)
        groups = {group.gr_name for group in grp.getgrall() if user in group.gr_mem or group.gr_gid == account.pw_gid}
        if account.pw_uid == 0 or groups & {"root", "docker", "sudo", "wheel", "admin", "adm", "systemd-journal", "lxd", "libvirt"}:
            raise ContractError("Codex account must not have root, container-daemon or administrative group access")
        if os.geteuid() != 0:
            raise ContractError("Worker must launch the distinct Codex account through root-owned runuser")
        return account

    @staticmethod
    def safe_path(path: Path) -> None:
        if not path.is_absolute() or path != path.resolve() or path.is_symlink():
            raise ContractError("Codex credential/session paths must be absolute without symlink components")

    @staticmethod
    def readable_ancestors(path: Path, account) -> None:
        groups = {group.gr_gid for group in grp.getgrall() if account.pw_name in group.gr_mem} | {account.pw_gid}
        for parent in path.parents:
            st = parent.stat()
            bit = 0o100 if st.st_uid == account.pw_uid else 0o010 if st.st_gid in groups else 0o001
            if not st.st_mode & bit:
                raise ContractError("codex_user cannot traverse session/socket parent: " + str(parent))

    def run(self, supervisor, service, *, recovery: str = "") -> dict:
        if supervisor.closed:
            return {"exit_code": None, "reason": "sealed", "finished": True, "session_id": None}
        if supervisor.cancelled.is_set():
            return {"exit_code": None, "reason": "cancelled", "finished": supervisor.closed, "session_id": None}
        skill = load_skill()
        source = Path(self.config.get("codex_home") or os.environ.get("CODEX_AI_CI_HOME", ""))
        self.safe_path(source)
        if not all((source / name).is_file() and not (source / name).is_symlink() for name in ("config.toml", "auth.json")):
            raise ContractError("Dedicated company Codex config.toml/auth.json are required")
        settings = load_toml(source / "config.toml")
        provider_name = settings.get("model_provider")
        provider = settings.get("model_providers", {}).get(provider_name, {})
        if not settings.get("model") or not provider.get("base_url") or provider.get("wire_api", "responses") != "responses":
            raise ContractError("Use the deployed company Responses provider and actual model")
        auth = json.loads((source / "auth.json").read_text())
        if not isinstance(auth, dict) or not (auth.get("OPENAI_API_KEY") or auth.get("tokens")):
            raise ContractError("Dedicated company auth.json contains no authentication material")
        account = self.account(self.config.get("codex_user"))
        if account.pw_uid == getattr(getattr(supervisor, "executor", None), "uid", None):
            raise ContractError("Codex host and candidate container must use different UIDs")
        task_id = supervisor.task["task_id"]
        if not re.fullmatch(r"[a-f0-9]{64}", task_id):
            raise ContractError("Invalid task identity")
        saved = supervisor.run_dir / "codex-session.json"
        provider_identity = hashlib.sha256(json.dumps({"model": settings["model"], "provider": provider_name,
                                                       "base_url": provider["base_url"]}, sort_keys=True).encode()).hexdigest()
        session_id = None
        if saved.exists():
            value = json.loads(saved.read_text())
            if (value.get("task_id") != task_id or value.get("provider_identity") != provider_identity
                    or value.get("skill_digest") != skill.manifest["digest"]
                    or not UUID.fullmatch(value.get("session_id", ""))):
                raise ContractError("Saved Codex session identity differs from this task/provider/Skill; resume with its trusted control revision")
            session_id = value["session_id"]
        session_root = Path(self.config.get("codex_sessions_root", str(self.state_dir / "codex-sessions")))
        self.safe_path(session_root)
        session_root.mkdir(parents=True, exist_ok=True)
        session_root.chmod(0o711)
        session = session_root / task_id
        home, workspace = session / "home", session / "workspace"
        for path in (session, home, workspace):
            self.safe_path(path)
            path.mkdir(exist_ok=True)
        session.chmod(0o711)
        home.chmod(0o700)
        workspace.chmod(0o755)
        self.readable_ancestors(home, account)
        self.readable_ancestors(Path(service.path), account)
        local_root = Path(__file__).resolve().parents[1]
        program = skill.prompt
        program_path = workspace / "TASK_SKILL.md"
        self.safe_path(program_path)
        program_path.write_text(program, encoding="utf-8")
        program_path.chmod(0o444)
        atomic_json(supervisor.run_dir / "skill-manifest.json", {"task_id": task_id, **skill.manifest})
        # Preserve the deployed model/provider, not unrelated hooks or MCP servers.
        effective = {key: value for key, value in settings.items() if key in MODEL_SETTINGS}
        finish_timeout = finish_timeout_seconds(self.config)
        effective.update({"model_providers": {provider_name: provider},
                          "sandbox_mode": "read-only", "approval_policy": "never", "web_search": "disabled",
                          "cli_auth_credentials_store": "file", "project_doc_max_bytes": 0,
                          "features": {"shell_tool": False, "unified_exec": False},
                          "mcp_servers": {"local_ci": {
                              "command": self.config.get("python_bin", "python3"),
                              "args": [str(local_root / "agent_ci/mcp_server.py")],
                              "env_vars": ["LOCAL_CI_RPC_SOCKET", "LOCAL_CI_RPC_TOKEN", "LOCAL_CI_FINISH_TIMEOUT_SECONDS"],
                              "required": True, "enabled": True, "enabled_tools": sorted(SCHEMAS),
                              "startup_timeout_sec": 30, "tool_timeout_sec": finish_timeout + 30,
                          }}})
        for name in ("config.toml", "auth.json"):
            self.safe_path(home / name)
        write_config(home / "config.toml", effective)
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
        (home / "auth.json").chmod(0o600)
        if os.geteuid() == 0:
            # Never recursively chown an account-writable session tree.
            for path in (home, home / "config.toml", home / "auth.json"):
                os.chown(path, account.pw_uid, account.pw_gid)
        child_env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(session),
                     "CODEX_HOME": str(home), "LANG": "C.UTF-8",
                     "LOCAL_CI_RPC_SOCKET": str(service.path), "LOCAL_CI_RPC_TOKEN": service.token,
                     "LOCAL_CI_FINISH_TIMEOUT_SECONDS": str(finish_timeout)}
        provider_env = [provider["env_key"]] if provider.get("env_key") else []
        provider_env += list(provider.get("env_http_headers", {}).values())
        for key in provider_env:
            if not isinstance(key, str) or key in child_env or key not in os.environ:
                raise ContractError("Required company provider environment is unavailable")
            child_env[key] = os.environ[key]
        for key in self.config.get("codex_proxy_env", []):
            if key not in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}:
                raise ContractError("Only explicit proxy variables may be inherited")
            if key in os.environ:
                child_env[key] = os.environ[key]
        prefix = [] if os.geteuid() == account.pw_uid else ["runuser", "-u", account.pw_name, "--"]
        executable = self.config.get("codex_bin", "codex")
        # CLI capability inspection is local and never calls a model.
        features = subprocess.run([*prefix, executable, "features", "list"], cwd=workspace,
                                  env=child_env, capture_output=True, text=True, timeout=30)
        supported = {line.split()[0] for line in features.stdout.splitlines() if line.strip()}
        if features.returncode or not {"shell_tool", "unified_exec"} <= supported:
            raise ContractError("Deployed Codex CLI cannot enforce the required tool boundary")
        effective["features"] = {name: False for name in sorted(DISABLED_FEATURES & supported)}
        write_config(home / "config.toml", effective)
        if os.geteuid() == 0:
            os.chown(home / "config.toml", account.pw_uid, account.pw_gid)
        # Resume has no --sandbox option. Global overrides apply to both forms.
        command = [*prefix, executable, "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
                   "-c", "features.shell_tool=false", "-c", "features.unified_exec=false", "-c", 'web_search="disabled"']
        prompt = program + "\n\n先调用 context，完成当前任务。所有工具只能作用于当前任务。"
        if recovery:
            prompt += "\n恢复信息（可信监督器）：" + recovery
        if session_id:
            command += ["exec", "resume", session_id, "--json", "--skip-git-repo-check", "-"]
        else:
            command += ["exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(workspace), "-"]
        output = supervisor.run_dir / ("codex-events-" + uuid.uuid4().hex + ".jsonl")
        started = time.monotonic()
        reason, offset, pending = "completed", 0, b""

        def consume_events():
            nonlocal offset, pending, session_id
            with output.open("rb") as reader:
                reader.seek(offset)
                chunk = reader.read()
                offset += len(chunk)
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get("type") == "thread.started" and UUID.fullmatch(str(event.get("thread_id", ""))):
                    if session_id and session_id != event["thread_id"]:
                        raise ContractError("Codex resumed a different session")
                    session_id = event["thread_id"]
                    atomic_json(saved, {"task_id": task_id, "session_id": session_id, "provider_identity": provider_identity,
                                        "skill_digest": skill.manifest["digest"]})

        with output.open("wb") as stream:
            process = subprocess.Popen(command, cwd=workspace, env=child_env, stdin=subprocess.PIPE,
                                       stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
                while process.poll() is None:
                    consume_events()
                    if supervisor.closed:
                        reason = "sealed"
                        break
                    if supervisor.cancelled.wait(0.2):
                        reason = "cancelled"
                        break
                    if time.monotonic() - started > self.config.get("codex_timeout_seconds", 21600):
                        reason = "timeout"
                        break
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                consume_events()
        return {"exit_code": process.returncode, "reason": reason,
                "duration_seconds": round(time.monotonic() - started, 3), "finished": supervisor.closed,
                "session_id": session_id, "event_log": str(output), "skill_digest": skill.manifest["digest"]}
