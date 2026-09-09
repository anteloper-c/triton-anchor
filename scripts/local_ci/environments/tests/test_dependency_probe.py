import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy import runtime_probe as probe
from environments.artifacts import tree_digest, EnvironmentError


def test_probe_uses_mounts_and_dependency_changes_revoke_proof(tmp_path):
    root = tmp_path / "dependencies"
    source = root / "ppl-v1"
    source.mkdir(parents=True)
    library = source / "runtime.so"
    library.write_bytes(b"fixture")
    root.chmod(0o755)
    source.chmod(0o755)
    library.chmod(0o644)
    entry = {"source": str(source), "target": "/opt/local-ci/runtime/deps/ppl",
             "read_only": True, "sha256": tree_digest(source)}
    settings = {
        "state_dir": str(tmp_path / "state"), "control_root": str(tmp_path / "control"),
        "dependency_root": str(root), "profiles": {"triton_v3.0": {"mounts": [entry]}},
        "runtime": {"kind": "docker-rootless", "endpoint": "unix:///run/user/1007/docker.sock", "context": "rootless"},
        "resources": {"cpus": 2, "memory_bytes": 1048576, "pids_limit": 64},
    }
    commands, labels = [], {}
    def docker(config, *args, **kw):
        commands.append(args)
        if args[0] == "create":
            labels.update(dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label"))
            return "b" * 64
        if args[0] == "start":
            return json.dumps({"cpu.max": "200000 100000", "memory.max": "1048576", "pids.max": "64"})
        if args[0] == "inspect":
            return json.dumps([{"Id": "b" * 64, "Config": {"Labels": labels},
                                "Mounts": [{"Destination": entry["target"], "Source": str(source), "Type": "bind", "RW": False}]}])
        assert args[0] == "rm"
        return ""
    with patch.object(probe, "runtime_status", return_value={}), \
         patch.object(probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}), \
         patch.object(probe, "docker", side_effect=docker), \
         patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="a" * 40)):
        proof = probe.probe_runtime(settings)
        assert probe.verify_probe(settings, {}) == proof
        library.write_bytes(b"replaced")
        with pytest.raises(EnvironmentError, match="SHA256 changed"):
            probe.verify_probe(settings, {})
    assert "--mount" in commands[0]
    assert entry["target"] == commands[0][-1]
    assert probe.DEPENDENCY_PROBE in commands[0][-2]
