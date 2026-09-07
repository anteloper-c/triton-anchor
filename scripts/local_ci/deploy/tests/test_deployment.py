from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("service_installer", DEPLOY / "install.py")
preflight = load("service_preflight", DEPLOY / "preflight.py")
health = load("health_publisher", DEPLOY / "health.py")
migration = load("migration_recorder", DEPLOY / "migrate.py")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.config = {"control_root": str(DEPLOY.parents[2]), "state_dir": str(self.root / "state"), "worker_id": "fixture",
                       "monitor_services": [],
                       "profiles": {"main": {"name": "fixture", "daily_calendar": "*-*-* 02:00:00 Asia/Shanghai"}}}

    def test_rendering_does_not_start_services_and_uses_private_env(self):
        units = installer.render_units(self.config, self.root / "config.json", self.root / "credentials.env")
        worker = units["triton-anchor-local-ci.service"]
        self.assertIn("agent_ci/worker.py", worker)
        self.assertIn("credentials.env", worker)
        self.assertNotIn("codex exec", worker)
        self.assertIn("OnCalendar=*-*-* 02:00:00 Asia/Shanghai", units["triton-anchor-local-ci-environment-fixture.timer"])
        self.assertNotIn("triton-anchor-local-ci.service", units["triton-anchor-local-ci-health.service"])

    def test_install_and_rollback_preserve_original_units(self):
        units = self.root / "units"
        units.mkdir()
        original = units / "triton-anchor-local-ci.service"
        original.write_text("old exact bytes\n")
        backup = self.root / "backup"
        installer.install_units({original.name: "new units\n", "triton-anchor-local-ci-health.timer": "timer\n"}, units, backup)
        self.assertEqual(original.read_text(), "new units\n")
        installer.rollback_units(backup, apply=True)
        self.assertEqual(original.read_text(), "old exact bytes\n")
        self.assertFalse((units / "triton-anchor-local-ci-health.timer").exists())

    def test_rollback_preserves_third_party_edits(self):
        units, backup = self.root / "units", self.root / "backup"
        installer.install_units({"triton-anchor-local-ci.service": "new"}, units, backup)
        target = units / "triton-anchor-local-ci.service"
        target.write_text("user changed")
        with self.assertRaisesRegex(ValueError, "changed since installation"):
            installer.rollback_units(backup, apply=True)
        self.assertEqual(target.read_text(), "user changed")

    def test_unit_path_injection_rejected(self):
        with self.assertRaises(ValueError):
            installer.render_units(self.config, Path("/tmp/config\nExecStart=bad"), self.root / "env")

    def test_sample_fails_missing_production_values(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        self.assertFalse(result["ready"])
        failed = {entry["check"] for entry in result["checks"] if entry["status"] == "fail"}
        self.assertIn("gitee_repo_url", failed)
        self.assertIn("profile:triton_v3.0:image", failed)
        self.assertIn("profile:triton_v3.0:daily_validation", failed)

    def test_collection_detects_stopped_poller_independently(self):
        state = Path(self.config["state_dir"])
        (state / "health").mkdir(parents=True)
        (state / "health/worker.json").write_text(json.dumps({"worker_id": "fixture", "heartbeat_at": 1000, "pid": os.getpid()}))
        class FakeManager:
            def health(self):
                return {"active": {}, "generations": []}
        result = health.collect(self.config, now=2000, manager=FakeManager())
        self.assertEqual(result["state"], "offline")
        self.assertTrue(result["poller"]["heartbeat_stale"])
        self.assertIn("filesystem_free_bytes", result["storage"][0])

    def test_migration_refuses_out_of_order_activation(self):
        state = migration.plan("a" * 40, "b" * 40)
        evidence = self.root / "evidence.json"
        evidence.write_text("{}")
        with self.assertRaisesRegex(ValueError, "recorded in order"):
            migration.advance(state, "poller_ready", evidence)

    def test_migration_requires_old_tasks_terminal_and_received(self):
        evidence = self.root / "old-result.json"
        evidence.write_text("fixture result")
        with self.assertRaisesRegex(ValueError, "nonterminal"):
            migration.drained([{"task_id": "old", "state": "running", "evidence_path": str(evidence)}])
        with self.assertRaisesRegex(ValueError, "confirmation"):
            migration.drained([{"task_id": "old", "state": "complete", "evidence_path": str(evidence)}])
        self.assertTrue(migration.drained([{"task_id": "old", "state": "complete", "receiver_confirmed": True, "evidence_path": str(evidence)}]))

    def test_migration_freezes_worker_identity_and_evidence_digest(self):
        state = migration.plan("a" * 40, "b" * 40)
        evidence = self.root / "compatibility.json"
        evidence.write_text(json.dumps({"receiver_accepts_v4": True, "legacy_results_display_only": True, "worker_preflight_ready": True, "worker_revision_sha": "a" * 40}))
        result = migration.advance(state, "compatibility_ready", evidence)
        self.assertEqual(result["next_phase"], "main_ready")
        self.assertEqual(len(result["events"][0]["evidence_sha256"]), 64)
        self.assertFalse(result["production_actions_executed"])

    def test_sessions_cannot_live_inside_trusted_worker_state(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        config["codex_sessions_root"] = config["state_dir"] + "/sessions"
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        check = next(row for row in result["checks"] if row["check"] == "session_state_separation")
        self.assertEqual(check["status"], "fail")


if __name__ == "__main__":
    unittest.main()
