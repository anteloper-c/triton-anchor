"""Real file/process tests for the merged execution lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.executor import LAUNCH_PROGRAM, STOP_PROGRAM, DockerExecutor
from agent_ci.protocol import ContractError, atomic_json
from agent_ci.state import Journal


def document():
    return {
        "task_id": "a" * 64,
        "repository": "likehupochuan/triton-anchor",
        "pr_number": 7,
        "target_branch": "main",
        "tested_sha": "b" * 40,
        "base_sha": "c" * 40,
        "llvm_hash": "d" * 40,
        "worker_revision_sha": "e" * 40,
    }


def test_completed_records_survive_partial_tail_and_process_restart(tmp_path):
    journal = Journal(tmp_path)
    task = document()
    journal.register(task)
    first = journal.execution(
        task["task_id"], "environment", "candidate", {"status": "pass", "exit_code": 0}
    )
    with (journal.run_dir(task["task_id"]) / "commands.jsonl").open("ab") as stream:
        stream.write(b'{"status":"pass"')
    reopened = Journal(tmp_path)
    assert [r["execution_id"] for r in reopened.executions(task["task_id"])] == [first]
    second = reopened.execution(
        task["task_id"],
        "frontend_build",
        "candidate",
        {"status": "fail", "exit_code": 1},
    )
    assert [r["execution_id"] for r in reopened.executions(task["task_id"])] == [
        first,
        second,
    ]
    assert not (tmp_path / "journal.sqlite3").exists()


def test_new_run_does_not_reuse_unsealed_installation(tmp_path):
    journal = Journal(tmp_path)
    task = document()
    old = journal.register(task)
    journal.execution(
        task["task_id"],
        "frontend_install",
        "candidate",
        {"status": "pass", "exit_code": 0},
    )
    new = journal.restart(task["task_id"])
    assert old["run_id"] != new["run_id"]
    assert journal.executions(task["task_id"]) == []
    assert (
        journal.run_dir(task["task_id"], old["run_id"]) / "commands.jsonl"
    ).is_file()


def test_sealed_retry_preserves_result_and_run_without_database(tmp_path):
    journal = Journal(tmp_path)
    task = document()
    row = journal.register(task)
    path = journal.run_dir(task["task_id"]) / "sealed/result.json"
    atomic_json(path, {"status": "pass"})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    journal.queue_result(task["task_id"], path, digest)
    journal.publication_failure(task["task_id"])
    assert journal.restart(task["task_id"])["run_id"] == row["run_id"]
    journal.resume(task["task_id"])
    assert journal.delivery(task["task_id"])["attempts"] == 1
    journal.published(task["task_id"])
    assert journal.task(task["task_id"])["phase"] == "published"
    assert json.loads(path.read_text())["status"] == "pass"
    with pytest.raises(ContractError):
        journal.queue_result(task["task_id"], path, "0" * 64)


def test_mutating_manifest_is_rejected(tmp_path):
    journal = Journal(tmp_path)
    task = document()
    journal.register(task)
    with pytest.raises(ContractError):
        journal.register({**task, "tested_sha": "f" * 40})


def test_one_group_cancel_preserves_same_uid_agent(tmp_path):
    processes = tmp_path / "processes"
    launcher = LAUNCH_PROGRAM.replace("/task/.processes", str(processes))
    stopper = STOP_PROGRAM.replace("/task/.processes", str(processes))
    agent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    command = subprocess.Popen(
        [
            sys.executable,
            "-c",
            launcher,
            sys.executable,
            "-c",
            "import time;time.sleep(60)",
        ],
        env={**os.environ, "LOCAL_CI_EXECUTION_ID": "check"},
    )
    try:
        deadline = time.time() + 5
        while not (processes / "check.json").exists() and time.time() < deadline:
            time.sleep(0.01)
        assert (processes / "check.json").exists()
        result = subprocess.run(
            [sys.executable, "-c", stopper, "check"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["verified"]
        command.wait(timeout=5)
        assert command.returncode != 0
        assert agent.poll() is None
    finally:
        for process in (command, agent):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_launcher_forks_before_setsid_when_parent_is_group_leader(tmp_path):
    processes = tmp_path / "processes"
    launcher = LAUNCH_PROGRAM.replace("/task/.processes", str(processes))
    command_script = (
        "import json,os,pathlib;"
        f"record=json.loads((pathlib.Path({str(processes)!r})/'check.json').read_text());"
        "print(json.dumps({"
        "'pid':os.getpid(),"
        "'pgrp':os.getpgrp(),"
        "'sid':os.getsid(0),"
        "'record_pid':record['pid'],"
        "'record_has_started_at':'started_at' in record"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher, sys.executable, "-c", command_script],
        env={**os.environ, "LOCAL_CI_EXECUTION_ID": "check"},
        capture_output=True,
        text=True,
        timeout=5,
        start_new_session=True,
        check=True,
    )
    identity = json.loads(result.stdout)
    assert identity["pid"] == identity["pgrp"] == identity["sid"]
    assert identity["record_pid"] == identity["pid"]
    assert identity["record_has_started_at"]


def test_process_identity_mismatch_refuses_to_signal(tmp_path):
    processes = tmp_path / "processes"
    processes.mkdir()
    (processes / "check.json").write_text(
        json.dumps({"pid": os.getpid(), "start": "wrong"})
    )
    stopper = STOP_PROGRAM.replace("/task/.processes", str(processes))
    result = subprocess.run(
        [sys.executable, "-c", stopper, "check"], capture_output=True
    )
    assert result.returncode != 0
    assert b"Process identity changed" in result.stderr


def executor(tmp_path):
    task = document()
    handle = {
        "run_id": "r1",
        "task_id": task["task_id"],
        "generation": "r1",
        "execution_uid": 11001,
        "execution_gid": 11001,
        "env": {
            "SEED_PYTHON": "/opt/venv/bin/python",
            "LLVM_BUILD_DIR": "/opt/llvm",
            "EXPECTED_TRITON_BACKEND": "fixture",
            "BACKEND_WHEEL_PATTERN": "backend*.whl",
            "BACKEND_TEST_COMMAND": "python smoke.py",
            "FLAGGEMS_CLONE_DIR": "/opt/flaggems",
        },
        "profile": "triton-3.0",
        "environment_fingerprint": "env1",
        "backend_enabled": True,
        "container_id": "fixture-container",
    }
    return DockerExecutor(
        {
            "max_jobs": 8,
            "runtime": {
                "kind": "docker-rootless",
                "endpoint": "unix:///tmp/test-docker.sock",
            },
            "profiles": {"main": {"triton_version": "3.0"}},
            "state_dir": str(tmp_path),
        },
        tmp_path,
        handle,
        task,
        None,
        manager=object(),
    )


def test_profile_bridge_uses_existing_b_environment_keys(tmp_path):
    ex = executor(tmp_path)
    context = ex.plan_context("f" * 32, "candidate", {})
    config = context["profile"]["tools"]
    assert config["backend_test_paths"] == ["tests"]
    assert config["expected_backend"] == "fixture"
    assert config["backend_smoke_argv"] == ["bash", "-c", "python smoke.py"]
    assert config["flaggems_dir"] == "/opt/flaggems"
    assert config["backend_dir"] == "/task/candidate/backend"
    assert context["triton_version"] == "3.0"


def test_budget_rejected_before_execution(tmp_path):
    ex = executor(tmp_path)
    with pytest.raises(ContractError):
        ex.environment("f" * 32, "candidate", {"jobs": 9})
    ex.generation["env"]["API_KEY"] = "secret"
    with pytest.raises(ContractError):
        ex.environment("f" * 32, "candidate", {})


def test_profile_backend_test_roots_preserve_explicit_overrides(tmp_path):
    ex = executor(tmp_path)
    ex.generation["env"]["BACKEND_TEST_PATHS"] = "unit_tests integration_tests"
    assert ex.plan_context("f" * 32, "candidate", {})["profile"]["tools"][
        "backend_test_paths"
    ] == ["unit_tests", "integration_tests"]
    ex.config["profiles"]["main"]["tools"] = {"backend_test_paths": ["custom_tests"]}
    assert ex.plan_context("f" * 32, "candidate", {})["profile"]["tools"][
        "backend_test_paths"
    ] == ["custom_tests"]


def test_custom_and_builtin_profile_setup_preserves_task_python(tmp_path):
    from tools.basic_tools.runner import environment_command

    setup = tmp_path / "setup.sh"
    setup.write_text(
        "export SDK_FIXTURE=loaded\nexport PYTHON_BIN=/wrong/python\nexport ANCHOR_DIR=/wrong/source\n"
    )
    tools_dir = str(Path(__file__).resolve().parents[2] / "tools")
    argv = environment_command(
        ["bash", "-c", 'printf "%s|%s|%s" "$SDK_FIXTURE" "$PYTHON_BIN" "$ANCHOR_DIR"'],
        {"env_scripts": [{"path": str(setup)}]},
        tools_dir,
    )
    result = subprocess.run(
        argv,
        env={**os.environ, "PYTHON_BIN": sys.executable, "ANCHOR_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "loaded|" + sys.executable + "|" + str(tmp_path)


def test_default_plan_jobs_respect_smaller_task_budget(tmp_path):
    from tools.basic_tools.runner import plan

    ex = executor(tmp_path)
    ex.config["max_jobs"] = 1
    specification = plan(
        "frontend_build", ex.plan_context("f" * 32, "candidate", {}), {}
    )
    assert all(
        command["env"]["MAX_JOBS"] == "1" for command in specification["commands"]
    )
