"""Verify the installed trusted implementation against the dispatched identity."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .protocol import ContractError


def validate_control_revision(config: dict, task: dict) -> str:
    if config.get("simulation") is True:
        return task["worker_revision_sha"]
    root = Path(config.get("control_root", "")).resolve()
    if not Path(__file__).resolve().is_relative_to(root / "scripts/local_ci"):
        raise ContractError(
            "Running worker is outside the configured trusted control checkout"
        )
    try:
        revision = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        dirty = subprocess.check_output(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                "scripts/local_ci",
                "scripts/ci",
                "scripts/api_contract",
                "api_contract",
                ".github",
            ],
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise ContractError("Trusted control checkout cannot be verified") from exc
    if revision != task["worker_revision_sha"] or dirty:
        raise ContractError(
            "Installed CI implementation differs from frozen worker_revision_sha; deploy the matching clean revision"
        )
    return revision
