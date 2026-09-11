"""Worker restart uses container stop/remove, never an old execution session."""

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.state import Journal
from agent_ci.workspaces import TaskWorkspaces


def fixture(tmp_path, *, state="stopped"):
    task = {
        "task_id": "a" * 64,
        "repository": "owner/repo",
        "pr_number": 1,
        "target_branch": "main",
    }
    journal = Journal(tmp_path)
    row = journal.register(task)
    run = journal.run_dir(task["task_id"])
    (run / "logs/command.log").write_text("durable execution output")
    (run / "artifacts/report.xml").write_text("<testsuite tests='1'/>")
    handle = {
        "task_id": task["task_id"],
        "run_id": row["run_id"],
        "attempt_id": row["run_id"],
        "state": state,
    }
    manager = SimpleNamespace(
        generations=lambda: {handle["attempt_id"]: handle},
        stop_task=Mock(
            return_value={"verified": True, "container_missing": state == "lost"}
        ),
        destroy_task=Mock(side_effect=lambda h, **kw: h.update(state="removed")),
        purge_credentials=Mock(
            side_effect=AssertionError("No session RPC after restart")
        ),
    )
    execution = SimpleNamespace(
        generation=handle,
        stop_task=Mock(side_effect=AssertionError("No old process table")),
        stop_codex=Mock(
            side_effect=AssertionError("docker exec fails in stopped container")
        ),
        export_native_evidence=Mock(
            side_effect=AssertionError("No old native session")
        ),
    )
    workspaces = TaskWorkspaces(
        {"state_dir": str(tmp_path)},
        journal,
        manager,
        None,
        lambda *args, **kwargs: execution,
    )
    return task, journal, run, handle, manager, execution, workspaces


@pytest.mark.parametrize("state", ["stopped", "lost", "running"])
def test_restart_stops_owned_environment_and_starts_new_unsealed_run(tmp_path, state):
    task, journal, run, handle, manager, execution, workspaces = fixture(
        tmp_path, state=state
    )
    original = journal.task(task["task_id"])["run_id"]
    workspaces.recover()
    assert workspaces.recovered
    manager.stop_task.assert_called_once_with(handle)
    manager.destroy_task.assert_called_once_with(handle, keep_data=False)
    execution.stop_codex.assert_not_called()
    assert journal.task(task["task_id"])["run_id"] != original
    previous = json.loads((run / "state.json").read_text())
    assert previous["abandoned"] and previous["cleanup"]["evidence_loss"]
    assert (run / "logs/command.log").read_text() == "durable execution output"
    assert (run / "artifacts/report.xml").exists()


@pytest.mark.parametrize("queued", [False, True])
def test_restart_after_seal_only_repairs_cleanup_and_delivery(tmp_path, queued):
    task, journal, run, handle, manager, execution, workspaces = fixture(tmp_path)
    sealed = run / "sealed/result.json"
    sealed.parent.mkdir()
    sealed.write_text('{"status":"pass"}')
    digest = hashlib.sha256(sealed.read_bytes()).hexdigest()
    if queued:
        journal.queue_result(task["task_id"], sealed, digest)
    original = journal.task(task["task_id"])["run_id"]
    workspaces.recover()
    assert workspaces.recovered
    assert journal.task(task["task_id"])["run_id"] == original
    assert journal.delivery(task["task_id"])["digest"] == digest
    assert journal.task(task["task_id"])["phase"] == "publish_pending"
    assert hashlib.sha256(sealed.read_bytes()).hexdigest() == digest
    execution.export_native_evidence.assert_not_called()


def test_failed_container_stop_does_not_rebuild_over_live_work(tmp_path):
    task, journal, run, handle, manager, execution, workspaces = fixture(tmp_path)
    original = journal.task(task["task_id"])["run_id"]
    manager.stop_task.return_value = {"verified": False}
    workspaces.recover()
    assert not workspaces.recovered
    assert journal.task(task["task_id"])["run_id"] == original
    manager.destroy_task.assert_not_called()
