from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops_maint import container_fs as fs
from ops_maint.artifacts import EnvironmentError
from ops_maint.runtime import EnvironmentManager, identities
from ops_maint.retention import retain_local
from ops_maint.health import public_snapshot
from ops_maint.watchdog import evaluate


def test_only_one_nonroot_execution_identity():
    assert identities({}) == ({"task": 11001}, {"task": 11001})
    for config in (
        {"identities": {"task": 0}},
        {"identities": {"candidate": 11001}},
        {"identities": {"gid": True}},
    ):
        with pytest.raises(EnvironmentError):
            identities(config)


def test_task_mounts_expose_only_work_and_artifacts(tmp_path):
    cfg = {
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
        },
        "resources": {"cpus": 2, "memory_bytes": 1024**3, "pids_limit": 100},
        "profiles": {},
    }
    manager = EnvironmentManager(cfg, tmp_path)
    image = dict(
        profile="test",
        image_release_id="r1",
        image_id="sha256:" + "a" * 64,
        llvm_hash="b" * 40,
        backend_enabled=False,
        env={},
        environment_fingerprint="f",
        daemon_id="d",
    )
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return ("c" * 64).encode() if args[0] == "create" else b""

    task = dict(
        task_id="a" * 64,
        target_branch="main",
        llvm_hash="b" * 40,
        worker_revision_sha="c" * 40,
    )
    with (
        patch.object(manager, "_control_revision", return_value="c" * 40),
        patch.object(manager, "ensure_image", return_value=image),
        patch.object(
            manager,
            "_control_snapshot",
            return_value={"path": "/fixed", "revision": "c" * 40},
        ),
        patch("ops_maint.runtime.control_mount_arguments", return_value=[]),
        patch.object(manager, "_verify"),
        patch.object(manager, "_helper", return_value={}),
        patch.object(manager, "_docker", side_effect=docker),
    ):
        handle = manager.acquire_task(task, "run-1")
    create = next(c for c in calls if c[0] == "create")
    mounts = [create[i + 1] for i, x in enumerate(create) if x == "--mount"]
    assert len(mounts) == 2
    assert mounts[0].endswith(",target=/task")
    assert mounts[1].endswith(",target=/task/artifacts")
    assert all("/sealed" not in m and "/logs" not in m for m in mounts)
    assert create[create.index("--user") + 1] == "11001:11001"
    assert handle["attempt_id"] == handle["run_id"] == "run-1"
    assert handle["rpc_container_dir"] == "/task/rpc"
    assert handle["uids"] == {"task": 11001}


def test_native_workspace_reuses_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "checkout").mkdir(parents=True)
    (candidate / "venv/bin").mkdir(parents=True)
    (candidate / "venv/bin/python").write_text("existing")
    (candidate / "venv/.local-ci-environment.json").write_text(
        json.dumps({"environment_fingerprint": "image-1"})
    )
    monkeypatch.setattr(fs, "TASK", tmp_path)
    monkeypatch.setattr(
        fs,
        "manifest",
        lambda: {"uids": {"task": os.getuid()}, "gids": {"task": os.getgid()}},
    )
    with patch.object(fs, "seed_venv") as seed:
        result = fs.prepare_native_workspace(
            {"expected_sha": "a" * 40, "environment_fingerprint": "image-1"}
        )
    assert result["checkout"] == str(candidate / "checkout")
    assert result["venv"] == str(candidate / "venv")
    seed.assert_not_called()
    assert not (tmp_path / "session/workspace").exists()


def make_run(root, run, phase, published):
    path = root / "runs" / ("a" * 64) / run
    (path / "artifacts").mkdir(parents=True)
    (path / "logs").mkdir()
    (path / "sealed").mkdir()
    (path / "artifacts/report.xml").write_text("result")
    (path / "logs/command.log").write_text("private log")
    (path / "sealed/result.json").write_text('{"verdict":"pass"}')
    (path / "state.json").write_text(
        json.dumps({"phase": phase, "delivery": {"published": published}})
    )
    return path


def test_retention_expires_only_published_and_preserves_summary(tmp_path):
    now = 40 * 86400
    expired = make_run(tmp_path, "old", "published", 86400)
    pending = make_run(tmp_path, "pending", "publish_pending", 86400)
    fresh = make_run(tmp_path, "fresh", "published", now - 86400)
    report = retain_local(
        {"state_dir": str(tmp_path), "state_min_free_bytes": 0}, now=now
    )
    assert len(report["expired"]) == 1 and not report["pause_intake"]
    assert not (expired / "artifacts").exists()
    assert (expired / "sealed/result.json").exists()
    assert (pending / "artifacts/report.xml").exists()
    assert (fresh / "logs/command.log").exists()


def test_retention_budget_pauses_without_deleting_required_pending(tmp_path):
    pending = make_run(tmp_path, "pending", "publish_pending", 0)
    report = retain_local(
        {"state_dir": str(tmp_path), "evidence_max_bytes": 1, "state_min_free_bytes": 0}
    )
    assert report["pause_intake"]
    assert (pending / "artifacts/report.xml").exists()


def test_health_whitelist_removes_nested_private_configuration():
    private = "PRIVATE_SENTINEL"
    result = public_snapshot(
        {
            "worker_id": "worker-1",
            "collected_at": "2026-09-10T00:00:00Z",
            "config": private,
            "runtime": {"endpoint": private, "available": True, "env": private},
            "environments": {
                "images": [{"env": private}],
                "runtime": {"endpoint": private},
            },
            "tasks": [
                {
                    "task_id": "a" * 64,
                    "run_id": "run-1",
                    "stage": "running",
                    "config": private,
                }
            ],
            "images": [
                {
                    "release_id": "image-1",
                    "image_id": "sha256:" + "a" * 64,
                    "state": "ready",
                    "env": private,
                    "log_path": private,
                }
            ],
            "storage": [{"filesystem_free_bytes": 10, "path": private}],
        }
    )
    assert private not in json.dumps(result)
    assert result["runtime"]["available"]


def snapshot(now):
    return {
        "worker_id": "worker-1",
        "collected_at": now.isoformat(),
        "state": "healthy",
        "poller": {"alive": True, "heartbeat_stale": False},
        "runtime": {"available": True, "rootless": True},
    }


def test_watchdog_unknown_does_not_clear_incident_or_claim_offline():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    broken = snapshot(now)
    broken["poller"]["alive"] = False
    first = evaluate({"workers": [broken]}, now=now)
    unknown = evaluate(
        {"source_error": True, "expected_workers": ["worker-1"]}, first, now=now
    )
    assert unknown["source_state"] == "unknown"
    assert unknown["active"] == first["active"]
    assert not any(r["code"] == "snapshot_stale" for r in unknown["active"].values())
    recovered = evaluate({"workers": [snapshot(now)]}, unknown, now=now)
    assert not recovered["active"]
    assert recovered["history"][-1]["transition"] == "recovered"
    assert (
        len(
            evaluate(
                {"workers": [snapshot(now)]},
                {"active": {}, "history": [{}] * 110},
                now=now,
            )["history"]
        )
        == 100
    )


def test_tracked_changes_record_patch_but_new_test_preserves_original(
    tmp_path, monkeypatch
):
    root = tmp_path / "candidate/checkout"
    root.mkdir(parents=True)
    (root / "code.py").write_text("original")
    control = tmp_path / ".control"
    control.mkdir()
    original = {"sha": "a" * 40, "files": {"code.py": fs.source_entry(root, "code.py")}}
    (control / "candidate-source-manifest.json").write_text(json.dumps(original))
    monkeypatch.setattr(fs, "TASK", tmp_path)
    monkeypatch.setattr(fs, "CONTROL", control)
    (root / "new_test.py").write_text("assert True")
    params = {"variant": "candidate", "expected_sha": "a" * 40}
    assert fs.verify_checkout(params)["verified"]
    (root / "code.py").write_text("modified")
    observed = fs.verify_checkout(params)
    assert not observed["verified"]
    assert observed["changed_files"] == ["code.py"]
    assert len(observed["patch_digest"]) == 64


def test_remote_expiry_publishes_index_before_attachment_delete(tmp_path):
    from ops_maint.retention import expire_delivery

    run = tmp_path / "runs" / ("a" * 64) / "run-1"
    run.mkdir(parents=True)
    events = []

    class Client:
        def attachments(self, release):
            events.append("list")
            return [{"id": 2}]

        def delete(self, release, attachment):
            events.append("delete")

    class Relay:
        results_branch = "local-ci-results"

        def write(self, branch, files):
            decoded = json.loads(next(iter(files.values())))
            assert decoded["status"] == "expired"
            events.append("publish")

    index = {
        "status": "ready",
        "artifacts": [
            {"status": "ready", "required": True, "release_id": 1, "attachment_id": 2}
        ],
    }
    expire_delivery({}, run, index, client=Client(), relay=Relay())
    assert events == ["publish", "list", "delete", "publish"]
    assert index["artifacts"][0]["deleted"]


def test_workspace_rejects_partial_and_changed_seed(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    (root / "venv/bin").mkdir(parents=True)
    (root / "venv/bin/python").write_text("existing")
    monkeypatch.setattr(fs, "TASK", tmp_path)
    monkeypatch.setattr(
        fs,
        "manifest",
        lambda: {"uids": {"task": os.getuid()}, "gids": {"task": os.getgid()}},
    )
    with pytest.raises(ValueError, match="Partial or different"):
        fs.prepare_workspace({"environment_fingerprint": "image-1"})
    (root / "venv/.local-ci-environment.json").write_text(
        json.dumps({"environment_fingerprint": "other"})
    )
    with pytest.raises(ValueError, match="Partial or different"):
        fs.prepare_workspace({"environment_fingerprint": "image-1"})


def test_retention_protects_unstarted_null_delivery(tmp_path):
    run = make_run(tmp_path, "preparing", "preparing", 0)
    (run / "state.json").write_text(
        json.dumps({"phase": "preparing", "delivery": None})
    )
    report = retain_local({"state_dir": str(tmp_path), "state_min_free_bytes": 0})
    assert report["protected"] == [{"task_id": "a" * 64, "run_id": "preparing"}]
    assert not report["errors"]


def cleanup_manager(tmp_path, *, container_id="missing"):
    settings = {
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
        },
        "resources": {"cpus": 1, "memory_bytes": 1024**3, "pids_limit": 100},
    }
    manager = EnvironmentManager(settings, tmp_path)
    work = tmp_path / "work" / ("a" * 64) / "run-1"
    work.mkdir(parents=True)
    (work / "scratch").write_text("temporary")
    artifacts = tmp_path / "runs" / ("a" * 64) / "run-1/artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "evidence").write_text("durable")
    handle = {
        "task_id": "a" * 64,
        "run_id": "run-1",
        "attempt_id": "run-1",
        "state": "stopped",
        "container": "local-ci-task-exact",
        "container_id": container_id,
        "image_id": "sha256:" + "a" * 64,
        "control_revision": "b" * 40,
        "control_snapshot": {},
        "daemon_id": "rootless-daemon",
        "workspace_host": str(work),
        "artifacts_host": str(artifacts),
    }
    state = manager._load()
    state["attempts"]["run-1"] = dict(handle)
    state["leases"][handle["task_id"]] = {"generation": "run-1"}
    manager._save(state)
    return manager, handle, work, artifacts


def test_missing_container_removes_only_owned_scratch(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    with (
        patch.object(manager, "_daemon", return_value="rootless-daemon"),
        patch.object(manager, "_docker", return_value=b""),
    ):
        manager.destroy_task(handle)
    assert not work.exists()
    assert (artifacts / "evidence").read_text() == "durable"
    assert manager.generation("run-1")["state"] == "removed"


def test_failed_missing_scratch_cleanup_stays_pending(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    with (
        patch.object(manager, "_daemon", return_value="rootless-daemon"),
        patch.object(manager, "_docker", return_value=b""),
        patch.object(
            manager,
            "_remove_scratch",
            side_effect=PermissionError("still owned by subuid"),
        ),
    ):
        with pytest.raises(PermissionError):
            manager.destroy_task(handle)
    assert manager.generation("run-1")["state"] != "removed"
    assert work.exists()


def test_uncertain_create_reconciles_exact_name_and_labels(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path, container_id=None)

    def docker(*args, **kwargs):
        return b"owned-id" if "--filter" in args else b""

    info = {
        "Image": handle["image_id"],
        "Config": {
            "Labels": {
                "local-ci.owner": manager.owner,
                "local-ci.kind": "task",
                "local-ci.attempt": "run-1",
            }
        },
    }
    with (
        patch.object(manager, "_docker", side_effect=docker),
        patch.object(manager, "_inspect", return_value=info),
        patch.object(manager, "_stop_owned", return_value={"verified": True}) as stop,
    ):
        manager.stop_task(handle)
    assert handle["container_id"] == "owned-id"
    assert manager.generation("run-1")["container_id"] == "owned-id"
    stop.assert_called_once_with("owned-id", "run-1")


def test_uncertain_create_rejects_unowned_name_collision(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path, container_id=None)
    with (
        patch.object(manager, "_docker", return_value=b"other"),
        patch.object(
            manager,
            "_inspect",
            return_value={"Image": handle["image_id"], "Config": {"Labels": {}}},
        ),
        patch.object(manager, "_stop_owned") as stop,
    ):
        with pytest.raises(EnvironmentError, match="unowned"):
            manager.stop_task(handle)
    stop.assert_not_called()
    assert manager.generation("run-1")["container_id"] is None


def test_subuid_scratch_uses_only_offline_work_management_mount(tmp_path):
    import shutil

    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    real_remove = shutil.rmtree
    calls = []
    first = True

    def remove(path):
        nonlocal first
        if first:
            first = False
            raise PermissionError("subuid")
        real_remove(path)

    def docker(*args, **kwargs):
        calls.append(args)
        return b"cleanup-id" if args[0] == "create" else b""

    with (
        patch("ops_maint.runtime.shutil.rmtree", side_effect=remove),
        patch("ops_maint.runtime.control_mount_arguments", return_value=[]),
        patch.object(manager, "_docker", side_effect=docker),
        patch.object(manager, "_stop_owned", return_value={"verified": True}),
    ):
        manager._remove_scratch(handle)
    create = next(c for c in calls if c[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    mounts = [create[i + 1] for i, x in enumerate(create) if x == "--mount"]
    assert mounts == ["type=bind,source=" + str(work) + ",target=/task"]
    assert not work.exists() and (artifacts / "evidence").exists()


@pytest.mark.parametrize("other_path", ["work", "work/other/run-1", "runs"])
def test_cleanup_rejects_any_path_except_this_task_run(tmp_path, other_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    handle["workspace_host"] = str(tmp_path / other_path)
    with patch.object(manager, "_docker") as docker:
        with pytest.raises(EnvironmentError, match="configured path"):
            manager._remove_scratch(handle)
    docker.assert_not_called()
    assert (work / "scratch").exists() and (artifacts / "evidence").exists()


def test_cleanup_reconciles_management_container_when_work_already_gone(tmp_path):
    import shutil

    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    shutil.rmtree(work)
    with (
        patch.object(
            manager, "_docker", side_effect=[b"previous-cleanup", b""]
        ) as docker,
        patch.object(manager, "_stop_owned") as stop,
    ):
        manager._remove_scratch(handle)
    stop.assert_called_once_with("previous-cleanup", "run-1", kind="task-cleanup")
    assert docker.call_args.args == ("rm", "previous-cleanup")
    assert (artifacts / "evidence").exists()


def test_health_excludes_abandoned_runs_from_active_task(tmp_path):
    from types import SimpleNamespace
    from ops_maint.health import collect

    now = 2_000_000_000
    old = make_run(tmp_path, "old", "running", 0)
    (old / "state.json").write_text(
        json.dumps({"phase": "running", "abandoned": True, "updated": now - 60})
    )
    current = make_run(tmp_path, "new", "preparing", 0)
    (current / "state.json").write_text(
        json.dumps({"phase": "preparing", "delivery": None, "updated": now})
    )
    (tmp_path / "health").mkdir()
    (tmp_path / "health/worker.json").write_text(
        json.dumps({"heartbeat_at": now, "pid": os.getpid()})
    )
    snapshot = collect(
        {"state_dir": str(tmp_path), "monitor_services": []},
        now=now,
        manager=SimpleNamespace(health=lambda: {}),
    )
    assert snapshot["active_task"]["run_id"] == "new"
    assert [t["run_id"] for t in snapshot["tasks"]] == ["new"]
