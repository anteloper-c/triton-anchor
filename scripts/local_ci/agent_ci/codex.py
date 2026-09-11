"""Run and resume Codex CLI with its native shell and administrator configuration."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from .credentials import validate_credentials
from .protocol import atomic_json


class CodexDriver:
    def __init__(self, config, state_dir):
        self.config, self.state_dir = config, Path(state_dir)
        self.secrets = {
            v
            for k, v in os.environ.items()
            if len(v) > 3
            and any(x in k.upper() for x in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        }

    def redact(self, text):
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    @staticmethod
    def client_environment():
        # The Docker client never needs Gitee or model credentials.
        return {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "HOME", "XDG_RUNTIME_DIR", "DOCKER_CONFIG", "LANG"}
        }

    def run(self, executor, *, cancelled, deadline, recovery=""):
        home = validate_credentials(
            Path(
                self.config.get("codex_home") or os.environ.get("CODEX_AI_CI_HOME", "")
            ),
            Path.home() / ".codex",
        )
        files = {
            name: (home / name).read_text(encoding="utf-8")
            for name in ("config.toml", "auth.json")
        }

        def remember(value):
            if isinstance(value, str) and len(value) > 3:
                self.secrets.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    remember(item)
            elif isinstance(value, list):
                for item in value:
                    remember(item)

        remember(json.loads(files["auth.json"]))
        program = (
            Path(self.config["control_root"]) / "scripts/local_ci/AI_CI_PROGRAM.md"
        ).read_text()
        files["AI_CI_PROGRAM.md"] = program
        env = executor.environment()
        env.update(
            CODEX_HOME="/task/session/home",
            LOCAL_CI_CODEX_WORKSPACE="/task/candidate/checkout",
        )
        # Provider and feature settings remain exactly as deployed. No MCP is injected.
        # Copy only credentials explicitly referenced by provider env_key settings.
        provider_keys = re.findall(
            r"(?m)^\s*env_key\s*=\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
            files["config.toml"],
        )
        for key in (
            *provider_keys,
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
                if key in provider_keys and len(env[key]) > 3:
                    self.secrets.add(env[key])
        executor.deploy_session(files, env)
        saved = executor.run_dir / "codex-session.json"
        session_id = (
            json.loads(saved.read_text()).get("session_id") if saved.exists() else None
        )
        arguments = [
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="danger-full-access"',
            "exec",
        ]
        if session_id:
            arguments += [
                "resume",
                "--json",
                "--output-last-message",
                "/task/artifacts/last-message.md",
                session_id,
                "-",
            ]
        else:
            arguments += [
                "--json",
                "--output-last-message",
                "/task/artifacts/last-message.md",
                "-",
            ]
        prompt = (
            program
            + "\n\nRead /task/artifacts/task-context.json and both tool context files. "
            "Perform this task and write /task/artifacts/agent-result.json.\n"
            + recovery
        )
        logs = executor.run_dir / "logs"
        logs.mkdir(exist_ok=True)
        path = logs / "codex.jsonl"
        path.touch(mode=0o600, exist_ok=True)
        process = subprocess.Popen(
            executor.codex_command(arguments),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.client_environment(),
            start_new_session=True,
        )

        def read_output():
            with path.open("ab") as log, (logs / "progress.log").open("a") as progress:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                        if event.get("type") == "thread.started" and isinstance(
                            event.get("thread_id"), str
                        ):
                            atomic_json(saved, {"session_id": event["thread_id"]})
                            progress.write(
                                "\nCodex session: " + event["thread_id"] + "\n"
                            )
                        item = event.get("item", {})
                        if (
                            event.get("type") == "item.completed"
                            and item.get("type") == "agent_message"
                        ):
                            progress.write(str(item.get("text", "")) + "\n")
                        elif (
                            event.get("type") == "item.completed"
                            and item.get("type") == "command_execution"
                        ):
                            progress.write(
                                f"$ {item.get('command', '')}\nexit: {item.get('exit_code')}\n"
                            )
                        elif event.get("type") in {"error", "turn.failed"}:
                            progress.write(json.dumps(event, ensure_ascii=False) + "\n")
                        progress.flush()
                    except (ValueError, TypeError):
                        pass  # CLI diagnostics need not be JSON.

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        reason = ""
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
            while process.poll() is None:
                if cancelled.wait(0.2):
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
            if reason:
                try:
                    executor.stop_codex()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return {
                "exit_code": process.returncode,
                "reason": reason,
                "session_id": json.loads(saved.read_text()).get("session_id")
                if saved.exists()
                else None,
            }
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            reader.join(timeout=5)
            process.stdout.close()
