"""Current MCP contract: one runner, observed native credit, host-owned state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.protocol import ContractError
from agent_ci.state import Journal
from agent_ci.supervisor import Supervisor, ToolService
from tools.basic_tools.runner import TOOL_IDS


def task_document():
    return {
        "task_id": "a" * 64,
        "tested_sha": "c" * 40,
        "base_sha": "b" * 40,
        "worker_revision_sha": "d" * 40,
        "repository": "likehupochuan/triton-anchor",
        "pr_number": 7,
        "target_branch": "triton_v3.0",
    }


class LocalExecutor:
    """No compiler fixture: custom scripts are real subprocesses, builtins are bounded facts."""

    def __init__(self, root):
        self.root = root
        self.config = {"max_jobs": 8}
        self.generation = {
            "generation": "g1",
            "environment_fingerprint": "env1",
            "backend_enabled": False,
            "profile": "triton-3.0",
        }
        self.calls = []
        self.installation = "wheel1"
        self.original = True
        (root / "candidate").mkdir()
        (root / "candidate/README.md").write_text("source")
        self.task = task_document()

    def prepare(self, variant="candidate"):
        path = self.root / variant
        path.mkdir(exist_ok=True)
        return path

    def plan_context(self, ident, variant, parameters):
        return {
            "source_dir": str(self.root / variant),
            "artifact_dir": str(self.root / "artifacts" / ident),
            "task_id": self.task["task_id"],
            "target_sha": self.task["tested_sha"],
            "triton_version": "3.0",
            "environment_fingerprint": "env1",
            "profile": {"backend_enabled": False, "tools": {}},
            "expected_imports": {},
        }

    def diagnostic_context(self):
        return {}

    def current_identity(self, variant):
        return {"original": self.original, "installation_identity": self.installation}

    def stop(self, ident):
        return {"verified": True, "remaining": []}

    def run(self, tool, ident, variant, parameters, cancelled, custom=None):
        self.calls.append(tool)
        root = self.root / "artifacts" / ident / tool
        root.mkdir(parents=True)
        log = self.root / (ident + ".log")
        code = 0
        if custom:
            script = root / custom["name"]
            script.write_text(custom["content"])
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                env={**os.environ, "FIXTURE_VARIANT": variant},
            )
            code = proc.returncode
            log.write_bytes(proc.stdout + proc.stderr)
        else:
            log.write_text("completed fixture")
        return {
            "execution_id": ident,
            "tool_id": tool,
            "variant": variant,
            "status": "pass" if code == 0 else "fail",
            "exit_code": code,
            "original_subject": self.original,
            "environment_fingerprint": "env1",
            "workspace_generation": "g1",
            "installation_identity": self.installation,
            "artifact_dir": str(root),
            "log_path": str(log),
            "tested_sha": self.task["tested_sha"],
            "details": {},
            "scope": {},
        }


@pytest.fixture
def supervisor(tmp_path):
    journal = Journal(tmp_path / "state")
    task = task_document()
    journal.register(task)
    ex = LocalExecutor(tmp_path)
    policy = {
        "version": "unified",
        "capabilities": list(TOOL_IDS),
        "required_checks": ["environment"],
        "required_reviews": ["pr_info", "architecture"],
        "not_applicable": [],
        "required_parameters": {},
    }
    sup = Supervisor(task, policy, journal, ex, journal.run_dir(task["task_id"]))
    yield sup
    sup.close()


def completed(sup, tool, parameters=None):
    return sup.poll_check(
        sup.start_check(tool, "test", parameters=parameters)["execution_id"], 30
    )


def test_schema_rejects_host_command_override_before_queue(supervisor):
    with pytest.raises(TypeError):
        supervisor.start_check(
            "environment", "reason", parameters={}, custom={"command": "host"}
        )


def test_runner_rejects_unknown_parameters_without_duplicate_registry(supervisor):
    with pytest.raises(ValueError):
        supervisor.start_check("environment", "reason", parameters={"host": "root"})
    assert supervisor.journal.executions(supervisor.task["task_id"]) == []


def test_dependencies_then_rebuild_invalidate_consumers(supervisor):
    with pytest.raises(ContractError):
        supervisor.start_check("frontend_smoke", "needs installed wheel")
    for tool in ("environment", "frontend_build", "frontend_install", "frontend_smoke"):
        assert completed(supervisor, tool)["status"] == "pass"
    assert supervisor.fresh("frontend_smoke")
    supervisor.poll_check(
        supervisor.start_check("frontend_build", "rebuild", force=True)["execution_id"],
        30,
    )
    assert not supervisor.fresh("frontend_smoke")


def test_native_reinstall_invalidates_test_but_keeps_build_history(supervisor):
    for tool in ("environment", "frontend_build", "frontend_install", "frontend_smoke"):
        completed(supervisor, tool)
    supervisor.executor.installation = "wheel2"
    assert supervisor.fresh("frontend_build")
    assert not supervisor.fresh("frontend_smoke")


def test_gitee_unknown_blocks_new_execution(supervisor):
    supervisor.control_available.clear()
    with pytest.raises(ContractError, match="Gitee"):
        supervisor.start_check("environment", "reason")
    assert supervisor.executor.calls == []


def test_custom_script_runs_real_python_without_requiring_build(supervisor):
    result = supervisor.poll_check(
        supervisor.run_custom("probe.py", "print(6*7)", "python", "diagnose")[
            "execution_id"
        ],
        30,
    )
    assert result["status"] == "pass"
    assert "42" in supervisor.read_artifact(result["execution_id"])["content"]
    assert not supervisor.fresh("environment")


def test_completed_native_environment_can_replace_builtin(supervisor):
    ex = supervisor.executor
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {
                "id": "native1",
                "type": "command_execution",
                "command": "python inspect_environment.py",
            },
        }
    )
    ident = next(iter(supervisor.native))
    directory = supervisor.run_dir / "artifacts/native-environment"
    directory.mkdir(parents=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "task_id": supervisor.task["task_id"],
                "target_sha": supervisor.task["tested_sha"],
                "environment_fingerprint": "env1",
                "missing": [],
            }
        )
    )
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {
                "id": "native1",
                "type": "command_execution",
                "command": "python inspect_environment.py",
                "exit_code": 0,
                "aggregated_output": "ok",
            },
        }
    )
    record = supervisor.record_check(
        ident,
        "environment",
        "/task/artifacts/native-environment",
        "actual environment report",
    )
    assert record["status"] == "pass"
    assert supervisor.fresh("environment")
    reused = supervisor.start_check("environment", "reuse equivalent native evidence")
    assert reused["execution_id"] == record["execution_id"]
    assert record["source_execution_id"] == ident
    assert (
        supervisor.journal.latest(supervisor.task["task_id"], "native_command")[
            "execution_id"
        ]
        == ident
    )
    assert ex.calls == []


def test_native_exit_without_report_does_not_gain_credit(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "n", "type": "command_execution", "command": "true"},
        }
    )
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "n", "type": "command_execution", "exit_code": 0},
        }
    )
    directory = supervisor.run_dir / "artifacts/empty"
    directory.mkdir()
    record = supervisor.record_check(
        next(iter(supervisor.native)),
        "environment",
        "/task/artifacts/empty",
        "missing evidence",
    )
    assert record["status"] == "infra_error"
    assert not supervisor.fresh("environment")


def test_native_modified_source_is_recorded_without_passing_original(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {
                "id": "n",
                "type": "command_execution",
                "command": "patch and test",
            },
        }
    )
    supervisor.executor.original = False
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "n", "type": "command_execution", "exit_code": 0},
        }
    )
    assert next(iter(supervisor.native.values()))["original_subject"] is False


def test_missing_started_event_cannot_be_promoted(supervisor):
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "unknown", "type": "command_execution", "exit_code": 0},
        }
    )
    assert not supervisor.native


def test_finish_requires_reviews_and_seals_immutable_result(supervisor):
    completed(supervisor, "environment")
    result = supervisor.finish("missing architecture evidence")
    assert result["status"] == "infra_error"
    assert (
        supervisor.journal.task(supervisor.task["task_id"])["phase"]
        == "publish_pending"
    )
    assert (supervisor.run_dir / "sealed/execution-summary.json").is_file()
    assert (
        supervisor.finish("cannot rewrite")["summary"]
        == "missing architecture evidence"
    )


def test_long_path_rpc_and_private_methods(supervisor):
    directory = supervisor.run_dir / "work/rpc"
    directory.mkdir(parents=True)
    path = directory / "broker.sock"
    with ToolService(supervisor, path) as service:
        address = (
            f"/proc/self/fd/{service.socket_dir_fd}/broker.sock"
            if service.socket_dir_fd
            else str(path)
        )
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(address)
            client.sendall(
                json.dumps(
                    {"token": service.token, "method": "_start", "arguments": {}}
                ).encode()
                + b"\n"
            )
            response = json.loads(client.makefile("rb").readline())
        assert "error" in response
        assert path.exists()
    assert not path.exists()


def test_one_native_command_can_cover_multiple_stages(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {
                "id": "multi",
                "type": "command_execution",
                "command": "inspect and build",
            },
        }
    )
    ident = next(iter(supervisor.native))
    directory = supervisor.run_dir / "artifacts/multi"
    directory.mkdir()
    task = supervisor.task
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "task_id": task["task_id"],
                "target_sha": task["tested_sha"],
                "environment_fingerprint": "env1",
                "missing": [],
            }
        )
    )
    (directory / "wheels").mkdir()
    wheel = directory / "wheels/test.whl"
    wheel.write_bytes(b"wheel fixture")
    import hashlib

    (directory / "wheel.json").write_text(
        json.dumps(
            {
                "task_id": task["task_id"],
                "target_sha": task["tested_sha"],
                "environment_fingerprint": "env1",
                "wheel": str(wheel),
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
        )
    )
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "multi", "type": "command_execution", "exit_code": 0},
        }
    )
    env = supervisor.record_check(
        ident, "environment", "/task/artifacts/multi", "measured environment"
    )
    build = supervisor.record_check(
        ident, "frontend_build", "/task/artifacts/multi", "built wheel"
    )
    assert env["execution_id"] != build["execution_id"]
    assert supervisor.fresh("environment") and supervisor.fresh("frontend_build")
    assert len(supervisor.journal.executions(task["task_id"])) == 3


def environment_report(supervisor, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "task_id": supervisor.task["task_id"],
                "target_sha": supervisor.task["tested_sha"],
                "environment_fingerprint": "env1",
                "missing": [],
            }
        )
    )


def test_native_true_cannot_credit_preexisting_reports(supervisor):
    directory = supervisor.run_dir / "artifacts/stale"
    environment_report(supervisor, directory)
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "stale", "type": "command_execution", "command": "true"},
        }
    )
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "stale", "type": "command_execution", "exit_code": 0},
        }
    )
    with pytest.raises(ContractError, match="not produced"):
        supervisor.record_check(
            next(iter(supervisor.native)),
            "environment",
            "/task/artifacts/stale",
            "old report",
        )


def test_background_late_report_cannot_credit_completed_shell(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "late", "type": "command_execution", "command": "inspect &"},
        }
    )
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "late", "type": "command_execution", "exit_code": 0},
        }
    )
    environment_report(supervisor, supervisor.run_dir / "artifacts/late")
    with pytest.raises(ContractError, match="not produced"):
        supervisor.record_check(
            next(iter(supervisor.native)),
            "environment",
            "/task/artifacts/late",
            "background report",
        )


def test_report_changes_after_completed_event_are_rejected(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "mutate", "type": "command_execution", "command": "inspect"},
        }
    )
    directory = supervisor.run_dir / "artifacts/mutate"
    environment_report(supervisor, directory)
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "mutate", "type": "command_execution", "exit_code": 0},
        }
    )
    with (directory / "environment.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(ContractError, match="changed afterward"):
        supervisor.record_check(
            next(iter(supervisor.native)),
            "environment",
            "/task/artifacts/mutate",
            "changed report",
        )


def test_interrupted_native_observation_is_preserved_and_does_not_block_sealing(
    supervisor,
):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {
                "id": "interrupted",
                "type": "command_execution",
                "command": "sleep 99",
            },
        }
    )
    supervisor.interrupt_native("Codex timeout")
    native = next(iter(supervisor.native.values()))
    assert native["status"] == "infra_error" and native["exit_code"] is None
    assert native["command"] == "sleep 99"
    assert (
        supervisor.journal.latest(supervisor.task["task_id"], "native_command")[
            "execution_id"
        ]
        == native["execution_id"]
    )
    assert (
        supervisor.finish("Incomplete validation remains incomplete")["status"]
        == "infra_error"
    )


def test_custom_reports_only_credit_fresh_own_execution_outputs(supervisor):
    import time

    ident = uuid.uuid4().hex
    directory = supervisor.run_dir / "artifacts" / ident
    started = time.time()
    environment_report(supervisor, directory)
    finished = time.time()
    record = {
        "execution_id": ident,
        "tool_id": "custom_probe",
        "variant": "candidate",
        "execution_kind": "custom",
        "status": "pass",
        "exit_code": 0,
        "started_at": started,
        "finished_at": finished,
        "artifact_dir": str(directory),
        "environment_fingerprint": "env1",
        "workspace_generation": "g1",
        "original_subject": True,
    }
    supervisor.journal.execution(
        supervisor.task["task_id"], "custom_probe", "candidate", record
    )
    result = supervisor.record_check(
        ident,
        "environment",
        "/task/artifacts/" + ident,
        "custom environment measurement",
    )
    assert result["status"] == "pass"
    environment_report(supervisor, supervisor.run_dir / "artifacts/unrelated")
    with pytest.raises(ContractError, match="belong"):
        supervisor.record_check(
            ident, "environment", "/task/artifacts/unrelated", "unrelated reports"
        )


def test_duplicate_completion_does_not_recapture_old_reports(supervisor):
    directory = supervisor.run_dir / "artifacts/stale-duplicate"
    environment_report(supervisor, directory)
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "duplicate", "type": "command_execution", "command": "true"},
        }
    )
    event = {
        "type": "item.completed",
        "item": {"id": "duplicate", "type": "command_execution", "exit_code": 0},
    }
    supervisor.observe_native(event)
    supervisor.observe_native(event)
    with pytest.raises(ContractError, match="not produced"):
        supervisor.record_check(
            next(iter(supervisor.native)),
            "environment",
            "/task/artifacts/stale-duplicate",
            "stale",
        )


def test_restarted_agent_item_ids_get_new_execution_identity(supervisor):
    started = {
        "type": "item.started",
        "item": {"id": "item_0", "type": "command_execution", "command": "true"},
    }
    completed = {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "command_execution", "exit_code": 0},
    }
    supervisor.observe_native(started)
    supervisor.observe_native(completed)
    supervisor.observe_native(started)
    supervisor.observe_native(completed)
    assert len(supervisor.native) == 2
    assert len(supervisor.journal.executions(supervisor.task["task_id"])) == 2


def test_completed_event_cannot_revive_interrupted_native_execution(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {
                "id": "cancelled",
                "type": "command_execution",
                "command": "sleep 99",
            },
        }
    )
    supervisor.interrupt_native("task cancelled")
    before = dict(next(iter(supervisor.native.values())))
    environment_report(supervisor, supervisor.run_dir / "artifacts/after-cancel")
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "cancelled", "type": "command_execution", "exit_code": 0},
        }
    )
    after = next(iter(supervisor.native.values()))
    assert after == before
    assert after["status"] == "cancelled" and after["observed_artifacts"] == {}


def test_custom_named_native_report_is_canonicalized_for_dependent_stages(supervisor):
    supervisor.observe_native(
        {
            "type": "item.started",
            "item": {"id": "named", "type": "command_execution", "command": "inspect"},
        }
    )
    directory = supervisor.run_dir / "artifacts/named"
    environment_report(supervisor, directory)
    (directory / "environment.json").rename(directory / "my-env.json")
    supervisor.observe_native(
        {
            "type": "item.completed",
            "item": {"id": "named", "type": "command_execution", "exit_code": 0},
        }
    )
    result = supervisor.record_check(
        next(iter(supervisor.native)),
        "environment",
        "/task/artifacts/named",
        "custom report",
        reports={"environment": "my-env.json"},
    )
    assert result["status"] == "pass"
    record = supervisor.journal.latest(supervisor.task["task_id"], "environment")
    assert (Path(record["artifact_dir"]) / "environment.json").is_file()


def test_sealing_redacts_public_result_and_log_without_changing_raw_evidence(
    supervisor,
):
    import gzip

    secret = "private-provider-token-example"
    supervisor.redact = lambda value: json.loads(
        json.dumps(value).replace(secret, "[REDACTED]")
    )
    record = completed(supervisor, "environment")
    raw = next(
        r
        for r in supervisor.journal.executions(supervisor.task["task_id"])
        if r["execution_id"] == record["execution_id"]
    )
    Path(raw["log_path"]).write_text("provider output " + secret)
    result = supervisor.finish("failure " + secret)
    assert secret not in (supervisor.run_dir / "sealed/result.json").read_text()
    assert "[REDACTED]" in result["summary"]
    assert secret in Path(raw["log_path"]).read_text()
    packed = list((supervisor.run_dir / "sealed").rglob("*.gz.part*"))
    assert packed
    assert all(
        secret.encode() not in gzip.decompress(path.read_bytes()) for path in packed
    )


def test_oom_retry_halves_runner_default_build_jobs(supervisor):
    completed(supervisor, "environment")
    original = supervisor.executor.run
    parameters_seen = []

    def run(tool, ident, variant, parameters, cancelled, custom=None):
        parameters_seen.append(parameters)
        result = original(tool, ident, variant, parameters, cancelled, custom)
        if len(parameters_seen) == 1:
            result.update(status="infra_error", reason="oom", exit_code=137)
        return result

    supervisor.executor.run = run
    result = completed(supervisor, "frontend_build")
    assert parameters_seen == [{}, {"jobs": 1}]
    assert result["status"] == "pass"
    assert result["retry_of"]


def test_oom_does_not_inject_build_parameters_into_test_tool(supervisor):
    for tool in ("environment", "frontend_build", "frontend_install"):
        completed(supervisor, tool)
    original = supervisor.executor.run
    calls = []

    def run(tool, ident, variant, parameters, cancelled, custom=None):
        calls.append(parameters)
        result = original(tool, ident, variant, parameters, cancelled, custom)
        result.update(status="infra_error", reason="oom", exit_code=137)
        return result

    supervisor.executor.run = run
    assert completed(supervisor, "frontend_tests")["status"] == "infra_error"
    assert calls == [{}]
