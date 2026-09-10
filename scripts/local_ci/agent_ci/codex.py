"""Dedicated server-only Codex session with a task-scoped MCP capability."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .protocol import ContractError, atomic_json
from .mcp_server import SCHEMAS
from .skill import load_skill
from .native_audit import NativeAudit

UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
DISABLED_FEATURES = {
    "apps", "hooks", "plugins", "browser_use",
    "computer_use", "multi_agent", "multi_agent_v2", "image_generation", "memories", "shell_snapshot",
    "skill_mcp_dependency_install", "code_mode_host", "js_repl", "python_repl",
}
REQUIRED_FEATURES = {"shell_tool", "unified_exec"}
ENABLED_FEATURES = REQUIRED_FEATURES | {"apply_patch_freeform"}
MODEL_SETTINGS = {
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "model_context_window", "model_auto_compact_token_limit",
}


def finish_timeout_seconds(config: dict) -> int:
    """Bound the entire seal RPC: three execution UIDs and pending evidence.

    Evidence exports vary with the task's execution count, so this is an
    explicit total budget rather than a claimed worst-case duration. Reserve
    at least three reaper calls, one management export and local sealing time.
    """
    cleanup = config.get("cleanup_timeout_seconds", 60)
    management = config.get("management_timeout_seconds", 600)
    seconds = config.get("finish_timeout_seconds", 3600)
    if any(type(value) is not int or value < 1 for value in (cleanup, management)):
        raise ContractError("Process cleanup and management timeouts must be positive integers")
    if type(seconds) is not int or not 1 <= seconds <= 86300:
        raise ContractError("finish_timeout_seconds must be an integer between 1 and 86300")
    if seconds < 3 * cleanup + management + 60:
        raise ContractError("finish_timeout_seconds must cover three process cleanups, one management export and 60 seconds of sealing")
    return seconds


def load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            from .credentials import parse_toml_fallback
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


def config_text(settings: dict) -> str:
    return "\n".join(json.dumps(key) + " = " + toml_value(value) for key, value in settings.items()) + "\n"


class CodexDriver:
    def __init__(self, config: dict, state_dir: Path):
        self.config, self.state_dir = config, Path(state_dir)

    @staticmethod
    def safe_path(path: Path) -> None:
        if not path.is_absolute() or path != path.resolve() or path.is_symlink():
            raise ContractError("Codex credential/session paths must be absolute without symlink components")

    @staticmethod
    def client_environment() -> dict[str, str]:
        # These are Docker-client settings, never model/MCP credentials. The
        # container launcher reads those from its private session volume.
        result = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
        for name in ("HOME", "XDG_RUNTIME_DIR", "DOCKER_CONFIG"):
            if name in os.environ:
                result[name] = os.environ[name]
        return result

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
        executor = supervisor.executor
        identities = executor.generation.get("uids", {})
        uids = [identities.get(role) for role in ("codex", "candidate", "base", "diagnostic")]
        if any(type(uid) is not int or uid <= 0 for uid in uids) or len(set(uids)) != 4:
            raise ContractError("Task Codex, candidate, base and diagnostic require four different non-root UIDs")
        task_id = supervisor.task["task_id"]
        if not re.fullmatch(r"[a-f0-9]{64}", task_id):
            raise ContractError("Invalid task identity")
        saved = supervisor.run_dir / "codex-session.json"
        attempt_id = executor.generation.get("attempt_id")
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", attempt_id):
            raise ContractError("Codex session requires a registered task attempt")
        provider_identity = hashlib.sha256(json.dumps({"model": settings["model"], "provider": provider_name,
                                                       "base_url": provider["base_url"]}, sort_keys=True).encode()).hexdigest()
        session_id = None
        if saved.exists():
            value = json.loads(saved.read_text())
            if (value.get("task_id") != task_id or value.get("provider_identity") != provider_identity
                    or value.get("skill_digest") != skill.manifest["digest"]
                    or not UUID.fullmatch(value.get("session_id", ""))):
                raise ContractError("Saved Codex session identity differs from this task/provider/Skill; resume with its trusted control revision")
            if value.get("attempt_id") == attempt_id:
                session_id = value["session_id"]
            else:
                # A new task volume has no old session transcript to resume.
                # Keep the pointer as history until a new thread.started event
                # arrives; repeat launch failures archive the same file once.
                history = supervisor.run_dir / "codex-session-history"
                history.mkdir(exist_ok=True)
                digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                atomic_json(history / (digest + ".json"), {**value, "archived_reason": "task_attempt_replaced",
                                                         "replacement_attempt_id": attempt_id})
                recovery = ("The previous task container/session volume was replaced. Start a new Codex session; "
                            "read context and revalidate invalidated checks in the current attempt. " + recovery)
        layout = executor.prepare_codex_session(files={}, environment={}, rpc_socket=Path(service.path))
        native = executor.prepare_native_workspace()
        home, workspace = layout["home"], native["checkout"]
        program = skill.prompt
        atomic_json(supervisor.run_dir / "skill-manifest.json", {"task_id": task_id, **skill.manifest})
        # Preserve the deployed model/provider, not unrelated hooks or MCP servers.
        effective = {key: value for key, value in settings.items() if key in MODEL_SETTINGS}
        finish_timeout = finish_timeout_seconds(self.config)
        effective.update({"model_providers": {provider_name: provider},
                          "sandbox_mode": "danger-full-access", "approval_policy": "never", "web_search": "disabled",
                          "cli_auth_credentials_store": "file", "project_doc_max_bytes": 0,
                          "features": {name: True for name in sorted(REQUIRED_FEATURES)},
                          "mcp_servers": {"local_ci": {
                              "command": layout["python_bin"],
                              "args": [layout["mcp_script"]],
                              "env_vars": ["LOCAL_CI_RPC_SOCKET", "LOCAL_CI_RPC_TOKEN", "LOCAL_CI_FINISH_TIMEOUT_SECONDS"],
                              "required": True, "enabled": True, "enabled_tools": sorted(SCHEMAS),
                              "startup_timeout_sec": 30, "tool_timeout_sec": finish_timeout + 30,
                          }}})
        child_env = {**executor.native_environment(native),
                     "CODEX_HOME": str(home), "LANG": "C.UTF-8",
                     "LOCAL_CI_CODEX_WORKSPACE": str(workspace),
                     "LOCAL_CI_RPC_SOCKET": layout["rpc_socket"], "LOCAL_CI_RPC_TOKEN": service.token,
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
        files = {"config.toml": config_text(effective), "auth.json": json.dumps(auth), "TASK_SKILL.md": program}
        executor.prepare_codex_session(files=files, environment=child_env, rpc_socket=Path(service.path))
        client_env = self.client_environment()
        # CLI capability inspection is local and never calls a model.
        try:
            features = subprocess.run(executor.codex_command(["features", "list"]), cwd=supervisor.run_dir,
                                      env=client_env, capture_output=True, text=True, timeout=30)
        finally:
            executor.stop_codex()
        # Removed flags can remain in `features list`; do not enable a retired
        # editing switch. Current CLIs expose ordinary editing without it.
        supported = {parts[0] for line in features.stdout.splitlines()
                     if len(parts := line.split()) >= 3 and parts[1] != "removed"}
        if features.returncode or not REQUIRED_FEATURES <= supported:
            raise ContractError("Deployed Codex CLI cannot enforce the required tool boundary")
        effective["features"] = {name: False for name in sorted(DISABLED_FEATURES & supported)}
        effective["features"].update({name: True for name in sorted(ENABLED_FEATURES & supported)})
        executor.prepare_codex_session(files={"config.toml": config_text(effective)},
                                       environment=child_env, rpc_socket=Path(service.path))
        # Resume has no --sandbox option. Global overrides apply to both forms.
        command = ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"',
                   "-c", "features.shell_tool=true", "-c", "features.unified_exec=true", "-c", 'web_search="disabled"']
        prompt = program + "\n\n先调用 context，完成当前任务。所有工具只能作用于当前任务。"
        prompt += "\n原生命令工作区（探索副本，不计入正式检查）：" + json.dumps(native, ensure_ascii=False)
        if recovery:
            prompt += "\n恢复信息（可信监督器）：" + recovery
        if session_id:
            command += ["exec", "resume", session_id, "--json", "--skip-git-repo-check", "-"]
        else:
            command += ["exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check", "--cd", str(workspace), "-"]
        output = supervisor.run_dir / ("codex-events-" + uuid.uuid4().hex + ".jsonl")
        native_root = supervisor.run_dir / "native" / output.stem
        native_root.mkdir(parents=True, mode=0o700)
        native_root.parent.chmod(0o700)
        atomic_json(native_root / "context.json", {"task_id": task_id, "attempt_id": attempt_id,
                    "tested_sha": supervisor.task.get("tested_sha"), "event_log": str(output)})

        def credential_strings(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [s for item in value.values() for s in credential_strings(item)]
            return []

        audit_path = native_root / "actions.jsonl"
        audit = NativeAudit(audit_path, {
            "task_id": task_id, "tested_sha": supervisor.task.get("tested_sha"),
            "attempt_id": attempt_id, "environment_fingerprint": executor.generation.get("environment_fingerprint"),
        }, output, secrets=[service.token, *credential_strings(auth), *(child_env[k] for k in provider_env)])
        started = time.monotonic()
        reason, offset, pending = "completed", 0, b""
        sealing_started_at = None

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
                audit.ingest(event)
                if isinstance(event, dict) and event.get("type") == "thread.started" and UUID.fullmatch(str(event.get("thread_id", ""))):
                    if session_id and session_id != event["thread_id"]:
                        raise ContractError("Codex resumed a different session")
                    session_id = event["thread_id"]
                    atomic_json(saved, {"task_id": task_id, "session_id": session_id, "provider_identity": provider_identity,
                                        "skill_digest": skill.manifest["digest"], "attempt_id": attempt_id})

        with audit, output.open("wb") as stream:
            output.chmod(0o600)
            process = subprocess.Popen(executor.codex_command(command), cwd=supervisor.run_dir, env=client_env, stdin=subprocess.PIPE,
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
                    now = time.monotonic()
                    if supervisor.closed:
                        reason = "sealed"
                        break
                    if getattr(supervisor, "sealing_started", False):
                        if sealing_started_at is None:
                            sealing_started_at = now
                        deadline = sealing_started_at + finish_timeout
                    else:
                        deadline = started + self.config.get("codex_timeout_seconds", 21600)
                    if now > deadline:
                        reason = "timeout"
                        break
            finally:
                try:
                    # Killing docker exec only stops its client. Reap the
                    # container's Codex UID and bridge even after a normal exit.
                    executor.stop_codex()
                    exported = executor.export_native_evidence(native_root / "workspace")
                    if not isinstance(exported, dict) or not (exported.get("exported") or exported.get("evidence_loss")):
                        raise ContractError("Native evidence export did not report completion")
                    atomic_json(native_root / "export.json", exported)
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
                "session_id": session_id, "event_log": str(output), "native_audit": str(audit_path),
                "native_evidence": str(native_root / "workspace"), "skill_digest": skill.manifest["digest"]}
