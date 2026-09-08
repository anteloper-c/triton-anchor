#!/usr/bin/env python3
"""Render/install systemd units, preserving config and backing up prior units.

Default is a reviewable plan. --apply installs only after successful production
preflight. --rollback restores the exact saved unit files. Neither mode modifies
the existing company model configuration, worker JSON, Docker or repositories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import check_configuration


def quoted(value: str) -> str:
    if any(character in value for character in "\n\r\x00"):
        raise ValueError("Systemd paths must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_units(config: dict, config_path: Path, credentials_path: Path) -> dict[str, str]:
    python = quoted(config.get("python_bin", "/usr/bin/python3"))
    root = Path(config["control_root"]) / "scripts/local_ci"
    common = f"EnvironmentFile={quoted(str(credentials_path))}\nWorkingDirectory={quoted(config['control_root'])}\nUMask=0077\n"
    worker = f"{python} {quoted(str(root / 'agent_ci/worker.py'))} --config {quoted(str(config_path))}"
    health = f"{python} {quoted(str(root / 'deploy/health.py'))} --config {quoted(str(config_path))} --publish"
    retention = f"{python} {quoted(str(root / 'maintenance/retain_results.py'))} --config {quoted(str(config_path))} --apply"
    units = {
        "triton-anchor-local-ci.service": "[Unit]\nDescription=Triton Anchor AI-driven Local CI worker\nAfter=network-online.target docker.service\nWants=network-online.target\n\n[Service]\nType=simple\n" + common + f"ExecStart={worker}\nRestart=always\nRestartSec=15\nTimeoutStopSec=60\nKillMode=mixed\n\n[Install]\nWantedBy=multi-user.target\n",
        "triton-anchor-local-ci-health.service": "[Unit]\nDescription=Publish independent Local CI worker health\nAfter=network-online.target\n\n[Service]\nType=oneshot\n" + common + f"ExecStart={health}\nTimeoutStartSec=10min\n",
        "triton-anchor-local-ci-health.timer": "[Unit]\nDescription=Refresh Local CI health independently of poller\n\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nRandomizedDelaySec=15\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
    }
    units["triton-anchor-local-ci-retention.service"] = "[Unit]\nDescription=Expire Local CI result evidence by upload age\nAfter=network-online.target\n\n[Service]\nType=oneshot\n" + common + f"ExecStart={retention}\nTimeoutStartSec=1h\n"
    units["triton-anchor-local-ci-retention.timer"] = "[Unit]\nDescription=Daily Local CI result retention\n\n[Timer]\nOnBootSec=30min\nOnUnitActiveSec=1d\nPersistent=true\nRandomizedDelaySec=5min\n\n[Install]\nWantedBy=timers.target\n"
    for branch, profile in config["profiles"].items():
        name = profile.get("name", branch.replace("/", "-"))
        if not __import__("re").fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", name):
            raise ValueError("Unsafe profile name")
        calendar = profile.get("daily_calendar", "")
        if not calendar or any(character in calendar for character in "\n\r"):
            raise ValueError("Each profile requires an explicit daily_calendar")
        stem = f"triton-anchor-local-ci-environment-{name}"
        command = f"{python} {quoted(str(root / 'deploy/rotate.py'))} --config {quoted(str(config_path))} --profile {quoted(name)}"
        units[stem + ".service"] = "[Unit]\nDescription=Validate and rotate persistent Local CI environment\nAfter=network-online.target docker.service\n\n[Service]\nType=oneshot\n" + common + f"ExecStart={command}\nTimeoutStartSec=24h\n"
        units[stem + ".timer"] = f"[Unit]\nDescription=Staggered daily Local CI environment rotation\n\n[Timer]\nOnCalendar={calendar}\nPersistent=true\nRandomizedDelaySec=60\n\n[Install]\nWantedBy=timers.target\n"
    return units


def install_units(units: dict[str, str], destination: Path, backup: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"schema": "triton-anchor-local-ci-service-install/v1", "destination": str(destination.resolve()), "units": {}}
    if any((destination / name).is_symlink() for name in units):
        raise ValueError("Refusing to replace symlinked systemd units")
    for name, content in units.items():
        target = destination / name
        existed = target.is_file()
        if existed:
            shutil.copy2(target, backup / name)
        manifest["units"][name] = {"existed": existed, "installed_sha256": hashlib.sha256(content.encode()).hexdigest()}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for name, content in units.items():
        target = destination / name
        temporary = destination / ("." + name + ".install")
        temporary.write_text(content)
        temporary.chmod(0o644)
        os.replace(temporary, target)
    return manifest


def rollback_units(backup: Path, *, apply: bool = False) -> dict:
    manifest = json.loads((backup / "manifest.json").read_text())
    if manifest.get("schema") != "triton-anchor-local-ci-service-install/v1":
        raise ValueError("Invalid service backup manifest")
    destination = Path(manifest["destination"])
    for name, entry in manifest["units"].items():
        if not name.startswith("triton-anchor-local-ci") or Path(name).name != name:
            raise ValueError("Unsafe service backup entry")
        target = destination / name
        if target.is_symlink() or (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != entry["installed_sha256"]):
            raise ValueError("Installed unit has changed since installation; preserve it for manual review")
    if apply:
        for name, entry in manifest["units"].items():
            target = destination / name
            if entry["existed"]:
                shutil.copy2(backup / name, target)
            else:
                target.unlink(missing_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--credentials-env")
    parser.add_argument("--unit-dir", default="/etc/systemd/system")
    parser.add_argument("--backup-dir")
    parser.add_argument("--render-dir")
    parser.add_argument("--rollback")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.rollback:
            manifest = rollback_units(Path(args.rollback), apply=args.apply)
            if args.apply:
                subprocess.run(["systemctl", "daemon-reload"], check=True)
            print(json.dumps({"rollback": manifest, "applied": args.apply}, indent=2))
            return 0
        if not args.config or not args.credentials_env:
            parser.error("--config and --credentials-env are required")
        config_path, credentials = Path(args.config).resolve(), Path(args.credentials_env).resolve()
        config = json.loads(config_path.read_text())
        units = render_units(config, config_path, credentials)
        if args.render_dir:
            output = Path(args.render_dir)
            output.mkdir(parents=True, exist_ok=True)
            for name, content in units.items():
                (output / name).write_text(content)
        if args.apply:
            if os.geteuid() != 0:
                raise ValueError("Systemd installation requires the server administrator account")
            if not credentials.is_file() or credentials.stat().st_mode & 0o077:
                raise ValueError("Credentials EnvironmentFile must exist and be private (mode 600)")
            ready = check_configuration(config)
            if not ready["ready"]:
                print(json.dumps(ready, ensure_ascii=False, indent=2))
                return 1
            backup = Path(args.backup_dir) if args.backup_dir else Path(config["state_dir"]) / "deploy-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            manifest = install_units(units, Path(args.unit_dir), backup)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            print(json.dumps({"installed": manifest, "backup": str(backup), "services_started": False}, indent=2))
        else:
            print(json.dumps({"planned_units": units, "applied": False}, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Local CI service installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
