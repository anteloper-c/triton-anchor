"""Native CLI recovery and real process-group launch/cancellation behavior."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent_ci.codex import CodexDriver
from agent_ci.executor import LAUNCH_PROGRAM, STOP_PROGRAM


def test_cli_resumes_with_original_configuration_and_without_service_credentials(
    tmp_path, monkeypatch
):
    home = tmp_path / "ci-home"
    home.mkdir()
    settings = 'model="fixture"\n[features]\nmulti_agent=true\n[model_providers.company]\nenv_key="COMPANY_CREDENTIAL"\n'
    (home / "config.toml").write_text(settings)
    (home / "auth.json").write_text('{"OPENAI_API_KEY":"fixture-model-key"}')
    control = tmp_path / "control/scripts/local_ci"
    control.mkdir(parents=True)
    (control / "AI_CI_PROGRAM.md").write_text("Inspect and test this task.")
    monkeypatch.setenv("GITEE_TOKEN", "fixture-gitee-key")
    monkeypatch.setenv("COMPANY_CREDENTIAL", "fixture-provider-key")
    commands, sessions = [], []

    class Executor:
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        def environment(self):
            return {"PATH": os.defpath}

        def deploy_session(self, files, environment):
            sessions.append((files, environment))

        def codex_command(self, arguments):
            commands.append(arguments)
            return [
                sys.executable,
                "-c",
                'import json,os,sys;sys.stdin.read();assert "GITEE_TOKEN" not in os.environ;print(json.dumps({"type":"thread.started","thread_id":"fixture-session"}));sys.exit(0)',
            ]

    executor = Executor()
    driver = CodexDriver(
        {"codex_home": str(home), "control_root": str(tmp_path / "control")}, tmp_path
    )
    for attempt in range(2):
        assert (
            driver.run(
                executor, cancelled=threading.Event(), deadline=time.monotonic() + 5
            )["exit_code"]
            == 0
        )
    assert "resume" not in commands[0]
    assert "resume" in commands[1] and "fixture-session" in commands[1]
    assert sessions[0][0]["config.toml"] == settings
    assert "GITEE_TOKEN" not in sessions[0][1]
    assert sessions[0][1]["COMPANY_CREDENTIAL"] == "fixture-provider-key"
    assert (
        driver.redact("fixture-model-key fixture-gitee-key") == "[redacted] [redacted]"
    )


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
