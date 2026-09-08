#!/usr/bin/env python3
"""Read-only deployment checks. Missing production values are explicit errors."""
from __future__ import annotations

import argparse
import grp
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOCAL_ROOT))
from environments.manager import DIGEST_RE, NAME_RE, SHA_RE, safe_source
from maintenance.watchdog import smtp_configuration
from agent_ci.skill import load_skill
from agent_ci.protocol import ContractError


def check_configuration(config: dict, *, runtime: bool = True, require_notifications: bool = True) -> dict:
    checks = []
    def check(name, condition, message):
        checks.append({"check": name, "status": "pass" if condition else "fail", "message": message})
    def source(name, value):
        try:
            safe_source(value, name)
            check(name, True, "Server-owned source configured")
        except ValueError as exc:
            check(name, False, str(exc))
        except RuntimeError as exc:
            check(name, False, str(exc))
    for name in ("state_dir", "control_root", "codex_sessions_root"):
        value = config.get(name)
        check(name, isinstance(value, str) and Path(value).is_absolute(), "Configure an absolute dedicated server path")
    state_value, sessions_value = config.get("state_dir"), config.get("codex_sessions_root")
    if isinstance(state_value, str) and isinstance(sessions_value, str):
        state_path, sessions_path = Path(state_value).resolve(), Path(sessions_value).resolve()
        check("session_state_separation", not sessions_path.is_relative_to(state_path) and not state_path.is_relative_to(sessions_path), "Codex sessions and trusted worker state must use independent, non-overlapping directories")
    if config.get("control_root"):
        control = Path(config["control_root"])
        check("trusted_control", (control / "scripts/local_ci/agent_ci/worker.py").is_file() and (control / "scripts/local_ci/tools").is_dir(), "Trusted worker and tools must exist in control_root")
        try:
            skill = load_skill(control / "scripts/local_ci/skills/local-ci")
            check("trusted_skill", True, "Loaded Skill entry and references: " + skill.manifest["digest"])
        except ContractError as exc:
            check("trusted_skill", False, str(exc))
    source("gitee_repo_url", config.get("gitee_repo_url"))
    source("health_repo_url", config.get("health_repo_url"))
    profiles = config.get("profiles", {})
    check("profiles", isinstance(profiles, dict) and bool(profiles), "At least one explicit target-branch recipe is required")
    names = set()
    for branch, profile in profiles.items():
        prefix = f"profile:{branch}"
        name = profile.get("name", branch.replace("/", "-"))
        check(prefix + ":name", bool(NAME_RE.fullmatch(str(name))) and name not in names, "Profile names must be unique safe identifiers")
        names.add(name)
        check(prefix + ":llvm_hash", bool(SHA_RE.fullmatch(str(profile.get("llvm_hash", "")))), "Current exact LLVM revision is required")
        backend = profile.get("backend_enabled", False)
        check(prefix + ":backend", isinstance(backend, bool) and (not backend or str(profile.get("triton_version", "")).split(".")[:2] == ["3", "0"]), "Only current Triton 3.0 may enable backend capability")
        image = profile.get("image")
        check(prefix + ":image", isinstance(image, str) and bool(image) and "<" not in image, "Provide an actual approved image; daily rotation has no guessed image fallback")
        user = profile.get("execution_user", config.get("container_execution_user", ""))
        check(prefix + ":execution_user", isinstance(user, str) and bool(user) and user not in {"root", "0", "0:0"}, "Configure the actual non-root user present in the candidate image")
        root = profile.get("workspace_root")
        check(prefix + ":workspace_root", isinstance(root, str) and Path(root).is_absolute() and root != "/", "Use an absolute dedicated generation workspace root")
        env = profile.get("env", {})
        seed = env.get("SEED_PYTHON") or env.get("PYTHON_VENV_ACTIVATE")
        check(prefix + ":seed_python", isinstance(seed, str) and Path(seed).is_absolute(), "Provide an absolute seed Python or venv activation path; manager verifies build/setuptools/wheel/pybind11/PyYAML/pytest imports as the task user")
        llvm = profile.get("llvm", {})
        mode = llvm.get("mode")
        check(prefix + ":llvm_mode", mode in {"source", "archive"}, "Daily/new-LLVM preparation requires an archive or source recipe")
        if mode == "archive":
            source(prefix + ":llvm_source", llvm.get("archive", llvm.get("url")))
            check(prefix + ":llvm_checksum", bool(DIGEST_RE.fullmatch(str(llvm.get("sha256", "")))), "Trusted LLVM archive SHA256 is mandatory")
            check(prefix + ":llvm_provenance", llvm.get("commit") == profile.get("llvm_hash"), "Archive provenance must name the profile LLVM commit")
        elif mode == "source":
            source(prefix + ":llvm_source", llvm.get("repository"))
        required = {"environment", "frontend_build", "wheel_install_import", "frontend_smoke"}
        if backend:
            required.update({"backend_rebuild", "backend_smoke_jit"})
            backend_required = ("BACKEND_PATH", "BACKEND_PROFILE", "BACKEND_WHEEL_PATTERN", "BACKEND_TEST_COMMAND", "EXPECTED_TRITON_BACKEND", "FLAGGEMS_CLONE_DIR", "PPL_ROOT")
            check(prefix + ":backend_env", all(isinstance(env.get(key), str) and env[key].strip() for key in backend_required), "3.0 requires actual backend/FlagGems/PPL paths, profile, wheel filename pattern, discovery name and smoke/JIT command")
            backend_path = Path(env.get("BACKEND_PATH") or ".")
            workspace_path = Path(profile.get("workspace_container", "/workspace"))
            check(prefix + ":backend_workspace", backend_path.is_absolute() and backend_path != workspace_path and backend_path.is_relative_to(workspace_path) and ".." not in backend_path.parts, "Backend source must be inside the generation workspace for isolated task checkouts")
        validations = profile.get("validation_commands", {})
        check(prefix + ":daily_validation", isinstance(validations, dict) and required.issubset(validations) and all(isinstance(command, list) and command and all(isinstance(arg, str) for arg in command) for command in validations.values()), "Daily candidates must run required validation commands before promotion")
        schedule = profile.get("daily_calendar")
        check(prefix + ":daily_calendar", isinstance(schedule, str) and bool(schedule) and "\n" not in schedule, "Provide a staggered systemd OnCalendar value")
        env = profile.get("env", {})
        check(prefix + ":credential_boundary", not any(any(part in key for part in ("TOKEN", "PASSWORD", "API_KEY", "SECRET", "CODEX_HOME")) for key in env), "Model/publishing credentials must stay outside candidate containers")
    calendars = [profile.get("daily_calendar") for profile in profiles.values()]
    check("staggered_rotation", len(calendars) == len(set(calendars)), "Profiles must have distinct daily rebuild times; the resource lock also serializes builds")
    if require_notifications:
        try:
            smtp_configuration()
            check("smtp", True, "Mail transport configured; no message sent")
        except ValueError as exc:
            check("smtp", False, str(exc))
        health_env = config.get("health_token_env", "GITEE_HEALTH_TOKEN")
        check("health_publish_auth", bool(os.environ.get(health_env)), "Set the configured health publishing credential environment variable")
    if runtime:
        check("linux", sys.platform.startswith("linux"), "Worker deployment requires Linux/systemd/Docker")
        for executable in (config.get("python_bin", "python3"), config.get("codex_bin", "codex"), config.get("docker_bin", "docker"), "git", "systemctl", "runuser"):
            check("executable:" + executable, bool(shutil.which(executable)), "Required server executable must be installed")
        try:
            import tomllib  # noqa: F401
            toml_ready = True
        except ImportError:
            toml_ready = importlib.util.find_spec("tomli") is not None
        check("toml_parser", toml_ready, "Use Python 3.11+ or install tomli in the worker interpreter")
        user = config.get("codex_user", "")
        try:
            account = pwd.getpwnam(user)
            groups = {group.gr_name for group in grp.getgrall() if user in group.gr_mem or group.gr_gid == account.pw_gid}
            check("codex_user", account.pw_uid != 0 and not groups.intersection({"root", "docker", "sudo", "wheel", "admin", "adm", "systemd-journal", "lxd", "libvirt"}), "Codex account must be non-root without Docker, sudo, journal or host administration access")
            numeric_users = [profile.get("execution_user", config.get("container_execution_user", "")) for profile in profiles.values()]
            check("container_host_identity_separation", not any(str(value).split(":")[0] == str(account.pw_uid) for value in numeric_users), "Container task UID must differ from the host Codex UID; manager also probes actual image identities")
            if os.geteuid() == 0 and sessions_value:
                parents = [path for path in Path(sessions_value).parents if path.exists()]
                traversable = all(subprocess.run(["runuser", "-u", user, "--", "test", "-x", str(path)], capture_output=True).returncode == 0 for path in parents)
                check("session_parent_traversal", traversable, "Codex account must be able to traverse every existing session-root parent")
                if state_value and Path(state_value).exists():
                    writable = subprocess.run(["runuser", "-u", user, "--", "test", "-w", state_value], capture_output=True).returncode == 0
                    check("worker_state_boundary", not writable, "Codex account must not write trusted worker state")
                mcp = Path(config.get("control_root", "")) / "scripts/local_ci/agent_ci/mcp_server.py"
                readable = subprocess.run(["runuser", "-u", user, "--", "test", "-r", str(mcp)], capture_output=True).returncode == 0
                check("codex_mcp_read_access", readable, "Codex account must read the trusted MCP script through its installed control-root parents")
        except KeyError:
            check("codex_user", False, "Create the explicitly configured dedicated Codex account before starting services")
        home_value = config.get("codex_home") or os.environ.get("CODEX_AI_CI_HOME", "")
        home = Path(home_value)
        home_valid = home.is_absolute() and (home / "config.toml").is_file() and (home / "auth.json").is_file()
        check("codex_credentials", home_valid, "Use the existing dedicated company model configuration and auth files")
        if home_valid:
            try:
                spec = importlib.util.spec_from_file_location("credential_validator", LOCAL_ROOT / "codex_ai/validate_codex_ai_credentials.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.validate_credentials(home, Path.home() / ".codex")
                check("company_provider", True, "Dedicated Responses provider/auth validated; no API request made and no values printed")
            except (Exception, SystemExit):
                check("company_provider", False, "Dedicated company credential/provider configuration failed validation")
        docker = config.get("docker_bin", "docker")
        if shutil.which(docker):
            result = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], text=True, capture_output=True, timeout=30)
            check("docker_daemon", result.returncode == 0, "Docker daemon must be available to the trusted worker")
    return {"schema": "triton-anchor-local-ci-preflight/v1", "ready": all(item["status"] == "pass" for item in checks), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--configuration-only", action="store_true")
    parser.add_argument("--skip-notifications", action="store_true", help="Configuration development only; production preflight must validate notification settings")
    args = parser.parse_args()
    try:
        result = check_configuration(json.loads(Path(args.config).read_text()), runtime=not args.configuration_only, require_notifications=not args.skip_notifications)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Local CI deployment preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
