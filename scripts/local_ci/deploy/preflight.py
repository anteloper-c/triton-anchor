#!/usr/bin/env python3
"""Read-only deployment checks, with an explicit trusted runtime-probe mode."""
from __future__ import annotations

import argparse
import grp
import json
import math
import os
import pwd
import re
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
from agent_ci.codex import finish_timeout_seconds
from deploy.runtime_probe import runtime_status, verify_probe, probe_runtime, validate_runtime_config
from environments.dependency_mounts import dependency_mounts, validate_mounted_llvm
from environments.runtime import validate_branch_profiles


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
    check("schema", config.get("schema") == "triton-anchor-local-ci-config/v2", "Use the explicit v2 Rootless Docker task-container configuration")
    check("removed_host_codex_user", "codex_user" not in config and "container_execution_user" not in config,
          "Remove host codex_user/container_execution_user; configure distinct container identities")
    check("codex_bin", isinstance(config.get("codex_bin"), str) and Path(config["codex_bin"]).is_absolute(), "Configure the actual absolute Codex path inside the trusted image")
    check("codex_home", isinstance(config.get("codex_home"), str) and Path(config["codex_home"]).is_absolute(), "Configure the existing private company model source; no endpoint or model is guessed")
    check("max_jobs", type(config.get("max_jobs", 8)) is int and config.get("max_jobs", 8) > 0, "Build parallelism defaults to 8 jobs and must be a positive integer")
    try:
        validate_runtime_config(config)
        check("runtime_resources_identities", True, "Explicit Rootless endpoint/context, CPU/memory/PID budgets and separate task UIDs configured")
    except ValueError as exc:
        check("runtime_resources_identities", False, str(exc))
    for name in ("state_dir", "control_root"):
        value = config.get(name)
        check(name, isinstance(value, str) and Path(value).is_absolute(), "Configure an absolute dedicated server path")
    check("one_way_delivery", "receipt_timeout_seconds" not in config,
          "Remove receipt_timeout_seconds: local completion is immutable Gitee upload; GitHub publication runs independently")
    retention = config.get("results_retention_days", 30)
    check("results_retention_days", type(retention) is int and retention > 0,
          "Keep uploaded result runs for a positive whole number of days; the default is 30")
    workspace_retention = config.get("task_workspace_retention_hours", 24)
    check("task_workspace_retention_hours", type(workspace_retention) in (int, float) and 0 <= workspace_retention <= 87600 and math.isfinite(workspace_retention),
          "Retain inactive failed or resumable task workspaces for 0 to 87600 finite hours; 0 requests immediate collection")
    workspace_budget = config.get("task_workspace_max_bytes", 100 * 1024**3)
    check("task_workspace_max_bytes", type(workspace_budget) is int and workspace_budget > 0,
          "Set a positive byte budget for task workspaces; active tasks and sealed outbox evidence remain protected")
    cleanup_timeout = config.get("cleanup_timeout_seconds", 60)
    check("cleanup_timeout_seconds", type(cleanup_timeout) is int and cleanup_timeout > 0,
          "Task process cleanup requires a positive whole-number timeout in seconds; the default is 60")
    try:
        deadline = finish_timeout_seconds(config)
        check("finish_timeout_seconds", True, "Bounded task sealing deadline configured: " + str(deadline) + " seconds")
    except ContractError as exc:
        check("finish_timeout_seconds", False, str(exc))
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
    try:
        validate_branch_profiles(config)
        check("branch_profiles", True, "Task branch aliases select existing profiles without changing task identity or LLVM")
    except RuntimeError as exc:
        check("branch_profiles", False, str(exc))
    names = set()
    for branch, profile in profiles.items():
        prefix = f"profile:{branch}"
        name = profile.get("name", branch.replace("/", "-"))
        check(prefix + ":name", bool(NAME_RE.fullmatch(str(name))) and name not in names, "Profile names must be unique safe identifiers")
        names.add(name)
        check(prefix + ":llvm_hash", bool(SHA_RE.fullmatch(str(profile.get("llvm_hash", "")))), "Current exact LLVM revision is required")
        backend = profile.get("backend_enabled", False)
        triton_30 = str(profile.get("triton_version", "")).split(".")[:2] == ["3", "0"]
        check(prefix + ":backend", isinstance(backend, bool) and backend == triton_30, "Triton 3.0 must enable backend capability; other current versions must disable it")
        image = profile.get("image")
        check(prefix + ":image", isinstance(image, str) and bool(re.fullmatch(r"(?:[^\s@]+@)?sha256:[a-f0-9]{64}", image)), "Provide the actual trusted foundation image by immutable SHA256 digest")
        check(prefix + ":removed_execution_user", "execution_user" not in profile and "existing_container" not in profile,
              "Task-container mode cannot adopt a persistent container or use the old shared execution_user")
        root = profile.get("workspace_root")
        check(prefix + ":workspace_root", isinstance(root, str) and Path(root).is_absolute() and root != "/", "Use an absolute logical source root for the trusted image recipe; this is not a persistent task workspace")
        env = profile.get("env", {})
        seed = env.get("SEED_PYTHON") or env.get("PYTHON_VENV_ACTIVATE")
        check(prefix + ":seed_python", isinstance(seed, str) and Path(seed).is_absolute(), "Provide an absolute seed Python or venv activation path; manager verifies build/setuptools/wheel/pybind11/PyYAML/pytest imports as the task user")
        llvm = profile.get("llvm", {})
        mode = llvm.get("mode")
        check(prefix + ":llvm_mode", mode in {"source", "archive", "mount"}, "LLVM requires an archive, source or verified read-only mount recipe")
        try:
            mounts = dependency_mounts(config, profile, verify_content=True)
            validate_mounted_llvm(profile, mounts)
            check(prefix + ":dependency_mounts", True, "Versioned CI-owned read-only dependencies verified")
        except (OSError, ValueError, RuntimeError) as exc:
            check(prefix + ":dependency_mounts", False, str(exc))
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
            check(prefix + ":backend_workspace", backend_path.is_absolute() and backend_path != workspace_path and backend_path.is_relative_to(workspace_path) and ".." not in backend_path.parts, "Backend source must be inside the image recipe logical container root for task-private copies")
        validations = profile.get("validation_commands", {})
        check(prefix + ":daily_validation", isinstance(validations, dict) and required.issubset(validations) and all(isinstance(command, list) and command and all(isinstance(arg, str) for arg in command) for command in validations.values()), "Trusted image candidates must run required validation commands before promotion")
        schedule = profile.get("daily_calendar")
        check(prefix + ":daily_calendar", isinstance(schedule, str) and bool(schedule) and "\n" not in schedule, "Provide a staggered systemd OnCalendar value")
        env = profile.get("env", {})
        check(prefix + ":credential_boundary", not any(any(part in key for part in ("TOKEN", "PASSWORD", "API_KEY", "SECRET", "CODEX_HOME")) for key in env), "Only the Codex-private task directory may receive model credentials; profile environment must contain no credentials")
    calendars = [profile.get("daily_calendar") for profile in profiles.values()]
    check("staggered_rotation", len(calendars) == len(set(calendars)), "Profiles must have distinct daily rebuild times; the resource lock also serializes builds")
    if require_notifications:
        try:
            smtp = smtp_configuration()
            check("smtp", True, "Mail transport configured; no message sent" if smtp else
                  "Optional SMTP is disabled; health publishing and GitHub PR notifications remain independent")
        except ValueError as exc:
            check("smtp", False, str(exc))
        check("gitee_publish_auth", bool(os.environ.get("GITEE_TOKEN", "").strip()),
              "Set GITEE_TOKEN for task/result repository access")
        health_env = config.get("health_token_env", "GITEE_HEALTH_TOKEN")
        check("health_publish_auth", bool(os.environ.get(health_env, "").strip()), "Set the configured health publishing credential environment variable")
    if runtime:
        check("linux", sys.platform.startswith("linux"), "Worker deployment requires Linux, user systemd and Rootless Docker")
        check("ordinary_ci_user", os.geteuid() != 0, "Run as the ordinary CI account, without sudo or root-owned runuser")
        try:
            account = pwd.getpwuid(os.getuid())
            groups = {group.gr_name for group in grp.getgrall() if account.pw_name in group.gr_mem or group.gr_gid == account.pw_gid}
            check("ci_account_groups", not groups.intersection({"root", "docker", "lxd", "libvirt"}),
                  "Manual sudo membership is allowed; root/rootful Docker/lxd/libvirt groups are not allowed for the CI runtime")
        except KeyError:
            check("ci_account_groups", False, "The ordinary CI account must be provisioned")
        for executable in (config.get("python_bin", "python3"), config.get("docker_bin", "docker"), "git", "systemctl"):
            check("executable:" + executable, bool(shutil.which(executable)), "Required host executable must be installed")
        if os.geteuid() != 0 and shutil.which("systemctl"):
            service = config.get("runtime", {}).get("service", "docker.service")
            if isinstance(service, str) and re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
                try:
                    result = subprocess.run(["systemctl", "--user", "show", service, "--property=LoadState,ActiveState"],
                                            text=True, capture_output=True, timeout=15)
                    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                    check("user_systemd", result.returncode == 0 and fields.get("LoadState") == "loaded" and fields.get("ActiveState") == "active",
                          "The configured Rootless Docker user service and user bus must be available")
                except (OSError, subprocess.TimeoutExpired):
                    check("user_systemd", False, "Cannot query the configured Rootless Docker user service")
            else:
                check("user_systemd", False, "Configure the actual Rootless Docker user service")
        for key in ("state_dir",):
            value = config.get(key)
            if isinstance(value, str) and Path(value).is_absolute():
                parent = next((path for path in [Path(value), *Path(value).parents] if path.exists()), None)
                check(key + ":ownership", bool(parent) and parent.stat().st_uid == os.getuid() and os.access(parent, os.W_OK | os.X_OK),
                      "Dedicated worker state must belong to and be writable by the CI account")
        home_value = config.get("codex_home", "")
        home = Path(home_value)
        files = [home / "config.toml", home / "auth.json"]
        home_valid = home.is_absolute() and all(path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid() and not path.stat().st_mode & 0o077 for path in files)
        check("codex_credentials", home_valid, "Configure actual private company model config/auth files owned by the CI account")
        if home_valid:
            try:
                from agent_ci.credentials import validate_credentials
                validate_credentials(home, Path.home() / ".codex")
                check("company_provider", True, "Existing company model/provider files validated; no API request made")
            except (Exception, SystemExit):
                check("company_provider", False, "Actual company model/provider configuration failed validation")
        try:
            info = runtime_status(config)
            check("rootless_runtime", True, "Explicit endpoint/context, rootless daemon and cgroup v2/systemd verified")
            evidence = verify_probe(config, info)
            check("runtime_probe", True, "Verified effective resource limits and trusted image capabilities: " + evidence["config_digest"])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            check("rootless_runtime_or_probe", False, str(exc))
    return {"schema": "triton-anchor-local-ci-preflight/v2", "ready": all(item["status"] == "pass" for item in checks), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--configuration-only", action="store_true")
    parser.add_argument("--skip-notifications", action="store_true", help="Configuration development only; production preflight must validate notification settings")
    parser.add_argument("--probe-runtime", action="store_true", help="Run an explicitly labelled trusted canary to prove effective limits and image dependencies; never starts services")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        if args.probe_runtime:
            if args.configuration_only:
                parser.error("--probe-runtime cannot be combined with --configuration-only")
            static = check_configuration(config, runtime=False, require_notifications=not args.skip_notifications)
            if not static["ready"]:
                print(json.dumps(static, ensure_ascii=False, indent=2))
                return 1
            probe_runtime(config)
        result = check_configuration(config, runtime=not args.configuration_only, require_notifications=not args.skip_notifications)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Local CI deployment preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
