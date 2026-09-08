"""Real state/Git/filesystem lifecycle with container and model boundaries replaced."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from agent_ci.protocol import ContractError, canonical, current_key, metadata_digest, task_id
from agent_ci.worker import Worker
from test_agent_ci import FakeCodex, FakeExecutor, FakeManager, Fixture


class ScratchExecutor(FakeExecutor):
    def prepare(self, variant="candidate"):
        checkout = super().prepare(variant)
        scratch = self.root / "venv"
        scratch.mkdir(exist_ok=True)
        (scratch / "installed-package.bin").write_bytes(b"x" * 8192)
        return checkout


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root)
        self.config = {"state_dir": str(self.root / "state"), "simulation": True,
                       "codex_attempts": 1, "retry_delay_seconds": 0, "rpc_socket_dir": str(self.root / "rpc")}
        self.manager, self.driver = FakeManager(self.root, backend=False), FakeCodex()
        FakeExecutor.calls, FakeExecutor.failures = [], {}
        self.worker = self.make_worker()

    def make_worker(self, executor=ScratchExecutor):
        return Worker(self.config, relay=self.fixture.relay, manager=self.manager,
                      driver=self.driver, executor_factory=executor)

    def scratch(self, task=None):
        return self.root / "workspace/tasks" / (task or self.fixture.task)["task_id"]

    def sealed(self):
        box = self.worker.journal.outbox(self.fixture.task["task_id"])
        return box, json.loads(Path(box["payload_path"]).read_text())

    def fail(self, status="fail"):
        FakeExecutor.failures = {"environment": [(status, "fixture_failure")]}
        self.worker.scan()

    def test_success_reclaims_venv_before_upload_and_preserves_evidence(self):
        self.worker.process(self.fixture.task)
        box, result = self.sealed()
        self.assertEqual("pass", result["status"])
        self.assertIsNone(box["published"])
        self.assertFalse(self.scratch().exists())
        self.assertTrue(list(Path(box["payload_path"]).parent.glob("evidence/*/execution.log")))
        records = self.worker.journal.executions(self.fixture.task["task_id"])
        self.assertTrue(all(r["reuse_invalidated"] for r in records))
        self.assertTrue(all((Path(r["artifact_dir"]) / "execution.log").is_file() for r in records))
        self.assertFalse((self.worker.state_dir / "workspace-evidence").exists())
        self.assertEqual({}, self.manager.leases())
        calls = self.driver.calls
        self.worker.scan()
        self.assertEqual(calls, self.driver.calls)
        self.assertEqual(box["digest"], self.sealed()[0]["digest"])

    def test_failed_upload_does_not_recreate_scratch_or_call_model(self):
        self.worker.process(self.fixture.task)
        calls = self.driver.calls
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=OSError("offline")):
            self.worker.scan()
        self.assertEqual(calls, self.driver.calls)
        self.assertFalse(self.scratch().exists())
        self.assertEqual("publishing", self.worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.worker.scan()
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])

    def test_failure_retention_expires_without_deleting_sealed_result(self):
        self.fail()
        self.assertTrue(self.scratch().exists())
        box, result = self.sealed()
        self.assertEqual("fail", result["status"])
        health = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("healthy", health["status"])
        self.assertFalse(self.scratch().exists())
        self.assertEqual(box["digest"], hashlib.sha256(Path(box["payload_path"]).read_bytes()).hexdigest())

    def test_disk_budget_evicts_retained_failure_before_deadline(self):
        self.fail()
        self.worker.workspaces.budget = 1
        health = self.worker.workspaces.collect()
        self.assertEqual("healthy", health["status"])
        self.assertEqual(0, health["logical_bytes"])
        self.assertFalse(self.scratch().exists())
        self.assertEqual("disk_budget", self.worker.workspaces.rows()[0]["reason"])

    def test_active_workspace_is_never_evicted_for_disk_budget(self):
        task = self.fixture.task
        self.worker.journal.register(task)
        generation = self.manager.acquire(task["task_id"], "triton_v3.0", task["llvm_hash"])
        self.worker.workspaces.attach(task, generation)
        executor = ScratchExecutor(self.config, self.worker.state_dir, generation, task, self.fixture.relay)
        executor.prepare()
        self.worker.workspaces.budget = 1
        health = self.worker.workspaces.collect(now=time.time() + 100 * 3600)
        self.assertEqual("error", health["status"])
        self.assertTrue(self.scratch().exists())

    def test_resume_after_collection_reexecutes_successful_prerequisites(self):
        FakeExecutor.failures = {"frontend_build": [("infra_error", "network_failure")]}
        self.worker.scan()
        old = self.worker.journal.latest(self.fixture.task["task_id"], "environment")["execution_id"]
        self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertFalse(self.scratch().exists())
        self.worker.journal.resume(self.fixture.task["task_id"])
        self.worker.scan()
        self.assertEqual("pass", self.sealed()[1]["status"])
        new = self.worker.journal.latest(self.fixture.task["task_id"], "environment")["execution_id"]
        self.assertNotEqual(old, new)

    def test_resume_before_expiry_reuses_live_installation(self):
        FakeExecutor.failures = {"frontend_build": [("infra_error", "network_failure")]}
        self.worker.scan()
        old = self.worker.journal.latest(self.fixture.task["task_id"], "environment")["execution_id"]
        self.worker.journal.resume(self.fixture.task["task_id"])
        self.worker.scan()
        new = self.worker.journal.latest(self.fixture.task["task_id"], "environment")["execution_id"]
        self.assertEqual(old, new)
        self.assertEqual("pass", self.sealed()[1]["status"])

    def test_reuse_check_failure_quarantines_and_prevents_green_seal(self):
        self.manager.validation_failure = "device has a remaining workload"
        self.worker.scan()
        self.assertEqual("infra_error", self.sealed()[1]["status"])
        self.assertTrue(self.manager.quarantined)
        self.assertIn("environment cleanup", " ".join(self.sealed()[1]["unfinished"]))

    def test_unconfirmed_process_stop_blocks_new_work(self):
        class CannotStop(ScratchExecutor):
            def stop_task(self):
                raise OSError("docker unavailable")
        self.worker = self.make_worker(CannotStop)
        with mock.patch.object(self.manager, "quarantine", return_value={"stopped": False}):
            self.worker.process(self.fixture.task)
            self.assertEqual("unsafe", self.worker.workspaces.rows()[0]["phase"])
            self.assertTrue(self.manager.leases())
            with self.assertRaisesRegex(ContractError, "maintenance blocks"):
                self.worker.process(self.fixture.task)

    def test_cleanup_failure_quarantines_without_destroying_evidence(self):
        self.fail()
        with mock.patch("agent_ci.workspaces.shutil.rmtree", side_effect=OSError("read-only scratch")):
            health = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", health["status"])
        self.assertTrue(self.manager.quarantined)
        self.assertTrue(Path(self.sealed()[0]["payload_path"]).is_file())
        self.assertEqual("healthy", self.worker.workspaces.collect(now=time.time() + 25 * 3600)["status"])

    def test_symlink_cannot_redirect_workspace_deletion(self):
        self.fail()
        original = self.scratch()
        moved = original.with_name(original.name + "-preserved")
        original.rename(moved)
        victim = self.root / "unrelated"
        victim.mkdir()
        (victim / "keep").write_text("keep")
        original.symlink_to(victim, target_is_directory=True)
        health = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", health["status"])
        self.assertEqual("keep", (victim / "keep").read_text())

    def test_recovery_reaps_original_generation_even_when_relay_is_down(self):
        task = self.fixture.task
        self.worker.journal.register(task)
        generation = self.manager.acquire(task["task_id"], "triton_v3.0", task["llvm_hash"])
        self.worker.workspaces.attach(task, generation)
        ScratchExecutor(self.config, self.worker.state_dir, generation, task, self.fixture.relay).prepare()
        calls = []
        class RecoveryExecutor(ScratchExecutor):
            def stop_task(self):
                calls.append(self.generation["generation"])
                return super().stop_task()
        restarted = self.make_worker(RecoveryExecutor)
        with mock.patch.object(self.fixture.relay, "refresh", side_effect=OSError("gitee unavailable")):
            with self.assertRaises(OSError):
                restarted.scan()
        self.assertEqual(["simulation-1", "simulation-1"], calls)
        self.assertEqual({}, self.manager.leases())
        self.assertTrue(self.scratch().exists())

    def test_superseded_pr_releases_lease_and_removes_old_scratch(self):
        task = self.fixture.task
        self.worker.journal.register(task)
        generation = self.manager.acquire(task["task_id"], "triton_v3.0", task["llvm_hash"])
        self.worker.workspaces.attach(task, generation)
        ScratchExecutor(self.config, self.worker.state_dir, generation, task, self.fixture.relay).prepare()
        changed = dict(task, title="new PR metadata")
        changed["metadata_digest"] = metadata_digest(changed)
        changed["task_id"] = task_id(changed)
        self.fixture.relay.write("local-ci-control", {f"current/{current_key(task)}.json": canonical({"task_id": changed["task_id"]})})
        restarted = self.make_worker()
        restarted.scan()
        self.assertFalse(self.scratch().exists())
        self.assertEqual("cancelled", restarted.journal.task(task["task_id"])["phase"])
        self.assertEqual({}, self.manager.leases())

    def test_upgrade_collects_old_completed_directory_without_lease(self):
        self.worker.scan()
        self.scratch().mkdir(parents=True)
        (self.scratch() / "old-venv").write_bytes(b"old dependencies")
        with self.worker.journal.connect() as db:
            db.execute("DELETE FROM task_workspaces")
        restarted = self.make_worker()
        restarted.scan()
        self.assertFalse(self.scratch().exists())
        self.assertEqual("removed", restarted.workspaces.rows()[0]["phase"])

    def test_restart_finishes_interrupted_directory_removal(self):
        self.fail()
        row = self.worker.workspaces.rows()[0]
        self.worker.workspaces.phase(row["task_id"], row["generation"], "cleaning", "interrupted_cleanup")
        restarted = self.make_worker()
        restarted.workspaces.recover()
        restarted.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertFalse(self.scratch().exists())
        self.assertTrue(Path(self.sealed()[0]["payload_path"]).is_file())

    def test_evidence_is_archived_before_generation_lease_release(self):
        release = self.manager.release
        observed = []
        def verify_release(task_id):
            for record in self.worker.journal.executions(task_id):
                self.assertFalse(Path(record["artifact_dir"]).is_relative_to(self.scratch()))
                self.assertTrue((Path(record["artifact_dir"]) / "execution.log").is_file())
            observed.append(task_id)
            release(task_id)
        with mock.patch.object(self.manager, "release", side_effect=verify_release):
            self.fail()
        self.assertEqual([self.fixture.task["task_id"]], observed)

    def test_interrupted_record_logs_survive_recovery_and_collection(self):
        task = self.fixture.task
        self.worker.journal.register(task)
        generation = self.manager.acquire(task["task_id"], "triton_v3.0", task["llvm_hash"])
        self.worker.workspaces.attach(task, generation)
        ScratchExecutor(self.config, self.worker.state_dir, generation, task, self.fixture.relay).prepare()
        ident = "b" * 32
        self.worker.journal.execution(task["task_id"], "environment", "candidate", {"execution_id": ident, "status": "running"})
        artifact = self.scratch() / "artifacts" / ident
        artifact.mkdir(parents=True)
        (artifact / "execution.log").write_text("last output before power loss")
        restarted = self.make_worker()
        restarted.workspaces.recover()
        restarted.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertFalse(self.scratch().exists())
        record = restarted.journal.latest(task["task_id"], "environment")
        self.assertEqual("infra_error", record["status"])
        self.assertEqual("last output before power loss", (Path(record["artifact_dir"]) / "execution.log").read_text())

    def test_low_disk_blocks_build_but_keeps_upload_retry(self):
        import shutil
        self.worker.process(self.fixture.task)
        original = shutil.disk_usage(self.worker.state_dir)
        exhausted = type(original)(original.total, original.total, 0)
        calls = self.driver.calls
        with mock.patch("agent_ci.workspaces.shutil.disk_usage", return_value=exhausted):
            self.worker.scan()
            self.assertEqual("error", self.worker.workspaces.collect()["status"])
        self.assertEqual(calls, self.driver.calls)
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])

    def test_nested_mount_is_never_recursively_deleted(self):
        import os
        self.fail()
        target = self.scratch() / "venv"
        original = os.path.ismount
        with mock.patch("agent_ci.workspaces.os.path.ismount", side_effect=lambda path: Path(path) == target or original(path)):
            health = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", health["status"])
        self.assertTrue((target / "installed-package.bin").is_file())


if __name__ == "__main__":
    unittest.main()
