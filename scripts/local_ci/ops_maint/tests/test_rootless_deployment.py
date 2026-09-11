from __future__ import annotations

import copy
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

LOCAL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL))
from ops_maint import health, install, runtime_probe as probe


def config(tmp_path):
    return {
        "schema": "triton-anchor-local-ci-config/v2",
        "state_dir": str(tmp_path / "state"),
        "control_root": str(LOCAL.parents[1]),
        "docker_bin": "fixture-docker",
        "codex_bin": "/usr/local/bin/codex",
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
            "context": "ci-rootless",
            "service": "docker.service",
        },
        "resources": {"cpus": 2, "memory_bytes": 134217728, "pids_limit": 64},
        "identities": dict(probe.DEFAULT_IDENTITIES),
        "monitor_services": [],
        "profiles": {
            "triton_v3.0": {
                "name": "triton-3.0",
                "image": "sha256:" + "a" * 64,
                "daily_calendar": "*-*-* 02:00:00 Asia/Shanghai",
            }
        },
    }


@pytest.mark.parametrize("value", [None, 0, -1, True, "2", float("inf"), float("nan")])
def test_invalid_cpu_budget_fails_before_docker(tmp_path, value):
    settings = config(tmp_path)
    settings["resources"]["cpus"] = value
    with pytest.raises(ValueError):
        probe.validate_runtime_config(settings)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "unix:///var/run/docker.sock",
        "tcp://127.0.0.1:2375",
        "unix:///tmp/docker.sock",
    ],
)
def test_system_or_ambient_endpoint_rejected(tmp_path, endpoint):
    settings = config(tmp_path)
    settings["runtime"]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        probe.validate_runtime_config(settings)


def test_every_docker_call_uses_fixed_host_and_drops_ambient_context(tmp_path):
    settings = config(tmp_path)
    with (
        patch.dict(
            os.environ,
            {
                "DOCKER_CONTEXT": "system-default",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            },
        ),
        patch.object(
            probe.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok"),
        ) as run,
    ):
        assert probe.docker(settings, "info") == "ok"
    argv = run.call_args.args[0]
    assert argv == ["fixture-docker", "--host", settings["runtime"]["endpoint"], "info"]
    assert "DOCKER_CONTEXT" not in run.call_args.kwargs["env"]
    assert "DOCKER_HOST" not in run.call_args.kwargs["env"]


@pytest.mark.parametrize(
    "kind,cgroup,driver,context_ok",
    [
        ("rootful", "2", "systemd", True),
        ("rootless", "1", "systemd", True),
        ("rootless", "2", "none", True),
        ("rootless", "2", "systemd", False),
    ],
)
def test_runtime_identity_and_controllers_are_checked(
    tmp_path, kind, cgroup, driver, context_ok
):
    settings = config(tmp_path)

    def docker(_config, *args):
        if args[0] == "context":
            return json.dumps(
                [
                    {
                        "Endpoints": {
                            "docker": {
                                "Host": settings["runtime"]["endpoint"]
                                if context_ok
                                else "unix:///var/run/docker.sock"
                            }
                        }
                    }
                ]
            )
        return json.dumps(
            {
                "SecurityOptions": ["name=" + kind],
                "CgroupVersion": cgroup,
                "CgroupDriver": driver,
                "ID": "fixture-daemon",
            }
        )

    with (
        patch.object(probe.os, "geteuid", return_value=1001),
        patch.object(probe.os, "getuid", return_value=1001),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(
            Path,
            "stat",
            return_value=SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=1001),
        ),
        patch.object(probe, "docker", side_effect=docker),
        pytest.raises(ValueError),
    ):
        probe.runtime_status(settings)


class FakeDocker:
    def __init__(self, limits=None, ownership=True):
        self.commands, self.labels = [], {}
        self.limits = limits or {
            "cpu.max": "200000 100000",
            "memory.max": "134217728",
            "pids.max": "64",
        }
        self.ownership = ownership

    def __call__(self, config, *args, **kwargs):
        self.commands.append(args)
        if args[0] == "create":
            self.labels = dict(
                args[index + 1].split("=", 1)
                for index, arg in enumerate(args)
                if arg == "--label"
            )
            return "b" * 64
        if args[0] == "start":
            return json.dumps(self.limits)
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "b" * 64,
                        "Config": {"Labels": self.labels if self.ownership else {}},
                    }
                ]
            )
        if args[0] == "rm":
            return ""
        raise AssertionError(args)


def test_canary_reads_effective_limits_and_binds_private_proof(tmp_path):
    settings, docker = config(tmp_path), FakeDocker()
    info = {
        "daemon_id": "fixture",
        "endpoint": settings["runtime"]["endpoint"],
        "uid": 1001,
    }
    images = {"triton_v3.0": "sha256:" + "c" * 64}
    with (
        patch.object(probe, "runtime_status", return_value=info),
        patch.object(probe, "active_images", return_value=images),
        patch.object(probe, "docker", docker),
    ):
        proof = probe.probe_runtime(settings)
        assert proof == probe.verify_probe(settings, info)
        changed = copy.deepcopy(settings)
        changed["resources"]["memory_bytes"] *= 2
        with pytest.raises(ValueError, match="configuration changed"):
            probe.verify_probe(changed, info)
        changed = copy.deepcopy(settings)
        changed["branch_profiles"] = {"CI_dev": "triton_v3.0"}
        with pytest.raises(ValueError, match="configuration changed"):
            probe.verify_probe(changed, info)
        with pytest.raises(ValueError, match="configuration changed"):
            probe.verify_probe(settings, {**info, "daemon_id": "replacement"})
    assert [args[0] for args in docker.commands] == ["create", "start", "inspect", "rm"]
    assert "--network" in docker.commands[0] and "none" in docker.commands[0]
    assert "--privileged" not in docker.commands[0]
    assert probe.proof_path(settings).stat().st_mode & 0o077 == 0


def test_active_image_selection_uses_real_public_registry_and_rejects_wrong_release(
    tmp_path,
):
    settings = config(tmp_path)
    settings["profiles"]["triton_v3.0"]["llvm_hash"] = "d" * 40
    manager = probe.EnvironmentManager(settings, settings["state_dir"])
    registry = {
        "schema": "triton-anchor-local-ci-environments/v2",
        "active_images": {"triton_v3.0": "release-1"},
        "images": {
            "release-1": {
                "release_id": "release-1",
                "target_branch": "triton_v3.0",
                "llvm_hash": "d" * 40,
                "image_id": "sha256:" + "c" * 64,
                "state": "ready",
                "validated": True,
            }
        },
        "attempts": {},
        "leases": {},
        "events": [],
    }
    manager.registry.write_text(json.dumps(registry))
    assert probe.active_images(settings) == {"triton_v3.0": "sha256:" + "c" * 64}
    for field, value in (
        ("validated", False),
        ("state", "quarantined"),
        ("llvm_hash", "e" * 40),
    ):
        changed = copy.deepcopy(registry)
        changed["images"]["release-1"][field] = value
        manager.registry.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="Build and validate"):
            probe.active_images(settings)


def test_missing_or_ineffective_limits_never_leave_passing_proof(tmp_path):
    settings = config(tmp_path)
    docker = FakeDocker(
        {"cpu.max": "max 100000", "memory.max": "max", "pids.max": "max"}
    )
    with (
        patch.object(probe, "runtime_status", return_value={}),
        patch.object(
            probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}
        ),
        patch.object(probe, "docker", docker),
        pytest.raises(ValueError, match="not effectively enforced"),
    ):
        probe.probe_runtime(settings)
    assert docker.commands[-1][0] == "rm"
    assert json.loads(probe.proof_path(settings).read_text())["status"] == "fail"


def test_failed_reprobe_revokes_old_passing_evidence(tmp_path):
    settings, docker = config(tmp_path), FakeDocker()
    info = {
        "daemon_id": "fixture",
        "endpoint": settings["runtime"]["endpoint"],
        "uid": 1001,
    }
    with (
        patch.object(probe, "runtime_status", return_value=info),
        patch.object(
            probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}
        ),
        patch.object(probe, "docker", docker),
    ):
        probe.probe_runtime(settings)
        docker.limits["pids.max"] = "max"
        with pytest.raises(ValueError, match="not effectively enforced"):
            probe.probe_runtime(settings)
        with pytest.raises(ValueError, match="incomplete"):
            probe.verify_probe(settings, info)


def test_probe_never_removes_replaced_or_unowned_container(tmp_path):
    settings, docker = config(tmp_path), FakeDocker(ownership=False)
    with (
        patch.object(probe, "runtime_status", return_value={}),
        patch.object(
            probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}
        ),
        patch.object(probe, "docker", docker),
        pytest.raises(ValueError, match="ownership changed"),
    ):
        probe.probe_runtime(settings)
    assert not any(command[0] == "rm" for command in docker.commands)
    assert json.loads(probe.proof_path(settings).read_text())["status"] == "fail"


def test_user_install_applies_only_units_and_user_daemon_reload(tmp_path):
    settings = config(tmp_path)
    config_file, credentials = tmp_path / "config.json", tmp_path / "credentials.env"
    config_file.write_text(json.dumps(settings))
    credentials.write_text("FIXTURE=private\n")
    credentials.chmod(0o600)
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config")}),
        patch.object(
            sys,
            "argv",
            [
                "install.py",
                "--config",
                str(config_file),
                "--credentials-env",
                str(credentials),
                "--apply",
            ],
        ),
        patch.object(install.os, "geteuid", return_value=1001),
        patch.object(install, "check_configuration", return_value={"ready": True}),
        patch.object(install.subprocess, "run") as run,
    ):
        assert install.main() == 0
    assert run.call_args_list[0].args[0] == ["systemctl", "--user", "daemon-reload"]
    assert len(run.call_args_list) == 1
    worker = (
        tmp_path / "config/systemd/user/triton-anchor-local-ci.service"
    ).read_text()
    assert "WantedBy=default.target" in worker
    assert "Requires=" not in worker and "User=root" not in worker


def test_user_install_requires_passing_preflight_without_installing_or_reloading(
    tmp_path,
):
    settings = config(tmp_path)
    config_file, credentials = tmp_path / "config.json", tmp_path / "credentials.env"
    config_file.write_text(json.dumps(settings))
    credentials.write_text("FIXTURE=private\n")
    credentials.chmod(0o600)
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config")}),
        patch.object(
            sys,
            "argv",
            [
                "install.py",
                "--config",
                str(config_file),
                "--credentials-env",
                str(credentials),
                "--apply",
            ],
        ),
        patch.object(install.os, "geteuid", return_value=1001),
        patch.object(
            install,
            "check_configuration",
            return_value={
                "ready": False,
                "checks": [{"check": "runtime_probe", "status": "fail"}],
            },
        ),
        patch.object(install.subprocess, "run") as run,
    ):
        assert install.main() == 1
    assert not run.called
    assert not (tmp_path / "config/systemd/user").exists()


def test_health_survives_docker_failure_and_uses_user_service_scope(tmp_path):
    settings = config(tmp_path)
    Path(settings["state_dir"]).mkdir()
    settings["monitor_services"] = ["triton-anchor-local-ci.service"]

    class Manager:
        def health(self):
            raise RuntimeError("Docker unavailable")

    with patch.object(
        health.subprocess,
        "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nSubState=running\n",
        ),
    ) as run:
        snapshot = health.collect(settings, manager=Manager())
    assert snapshot["environments"]["unavailable"] is True
    assert snapshot["images"] == []
    assert snapshot["service_scope"] == "user"
    assert run.call_args.args[0][:3] == ["systemctl", "--user", "show"]
