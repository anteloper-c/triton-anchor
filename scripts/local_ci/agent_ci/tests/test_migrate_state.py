"""Offline migration preserves immutable delivery and never reuses containers."""
import fcntl
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_ci import Fixture
from agent_ci.migrate_state import migrate
from agent_ci.protocol import ContractError, RESULT_SCHEMA, atomic_json
from agent_ci.state import Journal


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root)
        self.source, self.target = self.root / "old-state", self.root / "new-state"
        self.journal = Journal(self.source)
        self.row = self.journal.register(self.fixture.task)
        self.inventory = {"old_intake_stopped": True, "old_worker_stopped": True,
                          "old_containers_stopped": True, "leases_released": True, "tasks": []}

    def sealed(self, *, uploaded=False):
        result = self.source / "tasks" / self.row["task_id"] / self.row["run_id"] / "published/result.json"
        atomic_json(result, {"schema": RESULT_SCHEMA, "task": self.fixture.task,
                            "run_id": self.row["run_id"], "status": "pass"})
        evidence = result.parent / "evidence/command.log"
        evidence.parent.mkdir()
        evidence.write_text("immutable execution output\n")
        self.journal.queue_result(self.row["task_id"], result, hashlib.sha256(result.read_bytes()).hexdigest())
        if uploaded:
            self.journal.published(self.row["task_id"])
        return result

    def test_dry_run_does_not_create_target_or_change_source(self):
        self.sealed()
        before = {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        result = migrate(self.source, self.target, self.inventory)
        self.assertFalse(result["applied"])
        self.assertEqual(1, result["pending_uploads"])
        self.assertFalse(self.target.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    def test_pending_upload_is_remapped_with_identical_bytes(self):
        source_file = self.sealed()
        report = migrate(self.source, self.target, self.inventory, apply=True)
        migrated = Journal(self.target)
        box = migrated.outbox(self.row["task_id"])
        self.assertTrue(report["applied"])
        self.assertEqual("publishing", migrated.task(self.row["task_id"])["phase"])
        self.assertEqual(source_file.read_bytes(), Path(box["payload_path"]).read_bytes())
        self.assertTrue(Path(box["payload_path"]).is_relative_to(self.target))
        self.assertEqual([], migrated.executions(self.row["task_id"]))
        self.assertFalse((self.target / "environments.json").exists())

    def test_uploaded_task_stays_complete_and_second_import_is_idempotent(self):
        self.sealed(uploaded=True)
        first = migrate(self.source, self.target, self.inventory, apply=True)
        second = migrate(self.source, self.target, self.inventory, apply=True)
        self.assertEqual(first["digest"], second["digest"])
        self.assertTrue(second["already_applied"])
        self.assertEqual("complete", Journal(self.target).task(self.row["task_id"])["phase"])

    def test_unsealed_work_needs_explicit_cancellation(self):
        with self.assertRaisesRegex(ContractError, "explicitly cancelled"):
            migrate(self.source, self.target, self.inventory)
        self.inventory["tasks"] = [{"task_id": self.row["task_id"], "state": "cancelled", "reason": "Runtime migration"}]
        migrate(self.source, self.target, self.inventory, apply=True)
        self.assertEqual("cancelled", Journal(self.target).task(self.row["task_id"])["phase"])

    def test_incomplete_stop_evidence_is_rejected(self):
        self.sealed()
        for key in ("old_worker_stopped", "old_containers_stopped", "leases_released"):
            with self.subTest(key=key), self.assertRaises(ContractError):
                migrate(self.source, self.target, {**self.inventory, key: False})

    def test_overlapping_roots_and_symlink_evidence_are_rejected(self):
        result = self.sealed()
        with self.assertRaises(ContractError):
            migrate(self.source, self.source / "child", self.inventory)
        (result.parent / "leak").symlink_to(self.root / "source/README.md")
        with self.assertRaisesRegex(ContractError, "symlinks"):
            migrate(self.source, self.target, self.inventory)

    def test_digest_change_and_conflicting_target_are_rejected(self):
        result = self.sealed()
        original = result.read_bytes()
        result.write_text("{}")
        with self.assertRaisesRegex(ContractError, "digest"):
            migrate(self.source, self.target, self.inventory)
        result.write_bytes(original)
        Journal(self.target).register(self.fixture.task)
        with self.assertRaisesRegex(ContractError, "conflicting"):
            migrate(self.source, self.target, self.inventory, apply=True)

    def test_live_target_worker_lock_is_respected(self):
        self.sealed()
        self.target.mkdir()
        with (self.target / "poll.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ContractError, "target worker"):
                migrate(self.source, self.target, self.inventory, apply=True)

    def test_idempotent_import_still_checks_preserved_evidence(self):
        self.sealed(uploaded=True)
        migrate(self.source, self.target, self.inventory, apply=True)
        path = Path(Journal(self.target).outbox(self.row["task_id"])["payload_path"])
        path.write_text("{}")
        with self.assertRaisesRegex(ContractError, "no longer matches"):
            migrate(self.source, self.target, self.inventory, apply=True)

    def test_matching_task_run_does_not_allow_a_different_outbox(self):
        self.sealed()
        target = Journal(self.target)
        target.register(self.fixture.task)
        with target.connect() as db:
            db.execute("UPDATE tasks SET run_id=?", (self.row["run_id"],))
        competing = self.target / "other-result.json"
        atomic_json(competing, {"schema": RESULT_SCHEMA, "task": self.fixture.task,
                               "run_id": self.row["run_id"], "status": "fail"})
        digest = hashlib.sha256(competing.read_bytes()).hexdigest()
        target.queue_result(self.row["task_id"], competing, digest)
        with self.assertRaisesRegex(ContractError, "conflicting immutable outbox"):
            migrate(self.source, self.target, self.inventory, apply=True)
        self.assertEqual(digest, target.outbox(self.row["task_id"])["digest"])
        self.assertFalse((self.target / "tasks").exists())
        self.assertFalse((self.target / "task-container-migration.json").exists())

    def test_matching_task_run_cannot_leave_queued_work_beside_a_sealed_import(self):
        self.sealed()
        target = Journal(self.target)
        target.register(self.fixture.task)
        with target.connect() as db:
            db.execute("UPDATE tasks SET run_id=?", (self.row["run_id"],))
        with self.assertRaisesRegex(ContractError, "conflicting outbox presence"):
            migrate(self.source, self.target, self.inventory, apply=True)
        self.assertEqual("queued", target.task(self.row["task_id"])["phase"])
        self.assertIsNone(target.outbox(self.row["task_id"]))

    def test_interrupted_import_rejects_existing_execution_facts(self):
        self.sealed()
        migrate(self.source, self.target, self.inventory, apply=True)
        (self.target / "task-container-migration.json").unlink()
        target = Journal(self.target)
        target.execution(self.row["task_id"], "environment", "candidate", {"status": "pass", "execution_kind": "builtin"})
        with self.assertRaisesRegex(ContractError, "conflicting execution"):
            migrate(self.source, self.target, self.inventory, apply=True)
        self.assertEqual(1, len(target.executions(self.row["task_id"])))

    def test_interrupted_identical_import_preserves_an_already_completed_upload(self):
        self.sealed()
        migrate(self.source, self.target, self.inventory, apply=True)
        (self.target / "task-container-migration.json").unlink()
        target = Journal(self.target)
        target.published(self.row["task_id"])
        completed = target.outbox(self.row["task_id"])
        result = migrate(self.source, self.target, self.inventory, apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual("complete", target.task(self.row["task_id"])["phase"])
        self.assertEqual(completed, target.outbox(self.row["task_id"]))


if __name__ == "__main__":
    unittest.main()
