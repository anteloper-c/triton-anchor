"""Task-container reclamation, durable evidence and delivery recovery."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_ci import Fixture, FakeManager, FakeExecutor, FakeCodex
from agent_ci.worker import Worker


class ScratchExecutor(FakeExecutor):
    def prepare(self, variant="candidate"):
        path = super().prepare(variant)
        scratch = self.root / variant / "venv"
        scratch.mkdir(exist_ok=True)
        (scratch / "payload").write_bytes(b"x" * 4096)
        return path


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root)
        self.config = {"state_dir": str(self.root / "state"), "simulation": True,
                       "codex_attempts": 1, "retry_delay_seconds": 0, "rpc_socket_dir": str(self.root / "rpc"),
                       "minimum_free_bytes": 0}
        self.manager, self.driver = FakeManager(self.root), FakeCodex()
        FakeExecutor.calls, FakeExecutor.failures = [], {}
        self.worker = self.make_worker()

    def make_worker(self, executor=ScratchExecutor):
        return Worker(self.config, relay=self.fixture.relay, manager=self.manager, driver=self.driver,
                      executor_factory=executor)

    def row(self):
        return self.worker.workspaces.rows()[-1]

    def scratch(self):
        return Path(json.loads(self.row()["manifest"])["workspace_host"])

    def fail(self):
        FakeExecutor.failures = {"environment": [("fail", "fixture failure")]}
        self.worker.scan()
        self.assertEqual("fail", self.worker.journal.result_status(self.worker.journal.outbox(self.fixture.task["task_id"])))

    def test_success_removes_task_volume_and_staging_but_keeps_sealed_evidence(self):
        self.worker.scan()
        task_id = self.fixture.task["task_id"]
        self.assertEqual("complete", self.worker.journal.task(task_id)["phase"])
        self.assertEqual("removed", self.row()["phase"])
        self.assertFalse(self.scratch().exists())
        result = Path(self.worker.journal.outbox(task_id)["payload_path"])
        self.assertTrue(result.is_file())
        self.assertTrue(list((result.parent / "evidence").rglob("execution.log")))
        for record in self.worker.journal.executions(task_id):
            self.assertTrue(Path(record["artifact_dir"]).is_dir())

    def test_upload_failure_never_recreates_container_or_reinvokes_model(self):
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=OSError("relay down")):
            self.worker.scan()
        self.assertEqual("publishing", self.worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.assertEqual("removed", self.row()["phase"])
        before = (len(self.manager.acquired), self.driver.calls, len(FakeExecutor.calls))
        self.worker.scan()
        self.assertEqual(before, (len(self.manager.acquired), self.driver.calls, len(FakeExecutor.calls)))
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])

    def test_docker_recovery_failure_cannot_block_saved_upload(self):
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=OSError("relay down")):
            self.worker.scan()
        with mock.patch.object(self.worker.workspaces, "recover", side_effect=OSError("Docker unavailable")):
            self.worker.scan()
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.assertEqual("unavailable", json.loads((self.root / "state/health/worker.json").read_text())["runtime"])

    def test_socket_cleanup_failure_does_not_overwrite_sealed_result(self):
        from agent_ci.supervisor import ToolService
        original = ToolService.__exit__
        def broken(service, *args):
            original(service, *args)
            raise OSError("socket cleanup failed after finish")
        with mock.patch.object(ToolService, '__exit__', broken):
            self.worker.scan()
        box = self.worker.journal.outbox(self.fixture.task['task_id'])
        self.assertEqual('pass', self.worker.journal.result_status(box))
        self.assertEqual('complete', self.worker.journal.task(self.fixture.task['task_id'])['phase'])
        import hashlib
        self.assertEqual(box['digest'], hashlib.sha256(Path(box['payload_path']).read_bytes()).hexdigest())

    def test_interrupted_volume_artifact_is_exported_before_recovery_archive(self):
        def interrupted(supervisor, service, recovery=""):
            execution = supervisor.start_check("environment", "persist execution")
            supervisor.poll_check(execution['execution_id'], 30)
            self.worker.stop_event.set()
            return {'exit_code': 1}
        exports = []
        def export(handle, ident, target):
            exports.append(ident)
            (Path(target) / 'volume-only.ir').write_text('interrupted compiler artifact')
        with mock.patch.object(self.driver, 'run', side_effect=interrupted), mock.patch.object(self.manager, 'export_execution', side_effect=export):
            self.worker.scan()
        self.assertTrue(exports)
        records = self.worker.journal.executions(self.fixture.task['task_id'])
        self.assertTrue(any((Path(record['artifact_dir']) / 'volume-only.ir').is_file() for record in records))

    def test_unexported_volume_evidence_blocks_deletion(self):
        with mock.patch.object(self.manager, 'export_execution', side_effect=OSError('volume read failed')):
            self.worker.scan()
            self.assertEqual('unsafe', self.row()['phase'])
            self.assertTrue(self.scratch().exists())
            self.assertEqual('error', self.worker.workspaces.collect()['status'])
        self.worker.workspaces.recover()
        self.assertNotEqual('unsafe', self.row()['phase'])

    def test_failure_retention_expires_without_deleting_upload(self):
        self.fail()
        self.assertEqual("retained", self.row()["phase"])
        self.assertTrue(self.scratch().exists())
        self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("removed", self.row()["phase"])
        self.assertTrue(Path(self.worker.journal.outbox(self.fixture.task["task_id"])["payload_path"]).is_file())

    def test_disk_budget_evicts_retained_failure_before_ttl(self):
        self.fail()
        self.worker.workspaces.budget = 1
        result = self.worker.workspaces.collect()
        self.assertEqual("healthy", result["status"], result)
        self.assertEqual("disk_budget", self.row()["reason"])

    def test_active_task_is_not_evicted_to_meet_budget(self):
        task = self.fixture.task
        row = self.worker.journal.register(task)
        handle = self.manager.acquire_task(task, row["run_id"], rpc_directory=self.root / "rpc/task")
        self.worker.workspaces.attach(task, handle)
        executor = self.worker.make_executor(handle, task)
        executor.prepare()
        self.worker.workspaces.budget = 1
        result = self.worker.workspaces.collect(now=time.time() + 100 * 3600)
        self.assertEqual("error", result["status"])
        self.assertEqual("active", self.row()["phase"])
        self.assertTrue(self.scratch().exists())

    def test_explicit_rerun_gets_new_attempt_and_reexecutes_environment(self):
        FakeExecutor.failures = {"environment": [("infra_error", "fixture infrastructure outage")]}
        self.worker.scan()
        previous = self.row()["generation"]
        self.worker.journal.resume(self.fixture.task["task_id"])
        self.worker.scan()
        self.assertNotEqual(previous, self.row()["generation"])
        self.assertEqual(2, len([c for c in FakeExecutor.calls if c[0] == "environment"]))

    def test_worker_restart_preserves_attempt_and_valid_completed_stage(self):
        def interrupted(supervisor, service, recovery=""):
            execution = supervisor.start_check("environment", "Save real completed state")
            supervisor.poll_check(execution["execution_id"], 30)
            self.worker.stop_event.set()
            return {"exit_code": 1}
        with mock.patch.object(self.driver, "run", side_effect=interrupted):
            self.worker.scan()
        previous = self.row()["generation"]
        self.assertEqual("active", self.row()["phase"])
        self.assertEqual("queued", self.worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.worker = self.make_worker()
        self.worker.scan()
        self.assertEqual(previous, self.row()["generation"])
        self.assertEqual(1, len([c for c in FakeExecutor.calls if c[0] == "environment"]))
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])

    def test_restart_discovers_container_created_before_workspace_attach(self):
        task = self.fixture.task
        row = self.worker.journal.register(task)
        handle = self.manager.acquire_task(task, row['run_id'], rpc_directory=self.root / 'rpc/task')
        self.assertEqual([], self.worker.workspaces.rows())
        self.worker.workspaces.recover()
        self.assertEqual(handle['attempt_id'], self.row()['generation'])
        self.assertEqual('active', self.row()['phase'])
        self.worker.scan()
        self.assertEqual('complete', self.worker.journal.task(task['task_id'])['phase'])
        self.assertEqual(1, len(self.manager.acquired))

    def test_lost_volume_records_evidence_loss_and_does_not_reuse_old_check(self):
        task = self.fixture.task
        row = self.worker.journal.register(task)
        handle = self.manager.acquire_task(task, row['run_id'], rpc_directory=self.root / 'rpc/task')
        self.worker.workspaces.attach(task, handle)
        executor = self.worker.make_executor(handle, task)
        executor.prepare()
        ident = 'b' * 32
        artifact = executor.root / 'artifacts' / ident
        artifact.mkdir(parents=True)
        (artifact / 'execution.log').write_text('already saved output')
        self.worker.journal.execution(task['task_id'], 'environment', 'candidate', {'execution_id': ident, 'status': 'running'})
        with mock.patch.object(self.manager, 'recover_task', return_value={'status': 'rebuild_required'}), mock.patch.object(self.manager, 'export_execution', return_value={'exported': False, 'evidence_loss': 'task_volume_missing'}):
            self.worker.workspaces.recover()
            self.assertEqual('healthy', self.worker.workspaces.collect()['status'])
        record = self.worker.journal.executions(task['task_id'])[0]
        self.assertEqual('infra_error', record['status'])
        self.assertEqual('task_volume_missing', record['evidence_loss'])
        self.assertTrue(record['reuse_invalidated'])
        self.assertTrue((Path(record['artifact_dir']) / 'execution.log').is_file())
        self.worker.scan()
        self.assertEqual(2, len(self.manager.acquired))

    def test_unconfirmed_stop_blocks_next_task_without_destroying_evidence(self):
        self.manager.validation_failure = "stop could not be verified"
        self.worker.scan()
        self.assertEqual("unsafe", self.row()["phase"])
        self.assertTrue(self.scratch().exists())
        self.assertEqual("error", self.worker.workspaces.collect()["status"])
        self.manager.validation_failure = None
        self.worker.workspaces.recover()
        self.assertEqual("removed", self.row()["phase"])

    def test_cleanup_failure_is_retryable_and_keeps_immutable_result(self):
        self.fail()
        with mock.patch("agent_ci.workspaces.shutil.rmtree", side_effect=OSError("temporary I/O failure")):
            result = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", result["status"])
        self.assertEqual("cleanup_failed", self.row()["phase"])
        self.assertTrue(Path(self.worker.journal.outbox(self.fixture.task["task_id"])["payload_path"]).exists())
        self.assertEqual("healthy", self.worker.workspaces.collect(now=time.time() + 25 * 3600)["status"])

    def test_symlink_cannot_redirect_staging_cleanup(self):
        self.fail()
        root = self.scratch()
        protected = self.root / "protected"
        root.rename(protected)
        root.symlink_to(protected, target_is_directory=True)
        result = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", result["status"])
        self.assertTrue(protected.is_dir())

    def test_nested_mount_is_never_deleted(self):
        self.fail()
        nested = self.scratch() / "candidate/venv"
        original = os.path.ismount
        with mock.patch("agent_ci.workspaces.os.path.ismount", side_effect=lambda path: Path(path) == nested or original(path)):
            result = self.worker.workspaces.collect(now=time.time() + 25 * 3600)
        self.assertEqual("error", result["status"])
        self.assertTrue(nested.is_dir())

    def test_low_disk_blocks_intake_but_still_delivers_outbox(self):
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=OSError("relay down")):
            self.worker.scan()
        self.worker.config["minimum_free_bytes"] = 1
        disk = __import__("shutil").disk_usage(self.root)
        exhausted = type(disk)(disk.total, disk.total, 0)
        with mock.patch("agent_ci.workspaces.shutil.disk_usage", return_value=exhausted):
            self.worker.scan()
        self.assertEqual("complete", self.worker.journal.task(self.fixture.task["task_id"])["phase"])

    def test_legacy_workspace_requires_explicit_migration(self):
        task = self.fixture.task
        self.worker.journal.register(task)
        with self.worker.journal.connect() as db:
            db.execute("INSERT INTO task_workspaces VALUES(?,?,?,?,?,?,?)", (task["task_id"], "legacy",
                json.dumps({"workspace_host": str(self.root / "old-rootful")}), "active", time.time(), None, ""))
        self.worker.workspaces.recover()
        result = self.worker.workspaces.collect()
        self.assertEqual("error", result["status"])
        self.assertTrue(any("migration" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
