from __future__ import annotations

import importlib.util
import hashlib
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

    def test_preflight_loads_skill_and_rejects_partial_control_package(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        config['control_root'] = str(DEPLOY.parents[2])
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        skill = next(row for row in result['checks'] if row['check'] == 'trusted_skill')
        self.assertEqual('pass', skill['status'])
        config['control_root'] = str(self.root / 'partial-control')
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        skill = next(row for row in result['checks'] if row['check'] == 'trusted_skill')
        self.assertEqual('fail', skill['status'])
        self.assertIn('SKILL.md', skill['message'])

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

    def test_migration_drains_uploaded_old_tasks_without_github_confirmation(self):
        evidence = self.root / "old-result.json"
        evidence.write_text("fixture result")
        with self.assertRaisesRegex(ValueError, "nonterminal"):
            migration.drained([{"task_id": "old", "state": "running", "evidence_path": str(evidence)}])
        with self.assertRaisesRegex(ValueError, "upload"):
            migration.drained([{"task_id": "old", "state": "complete", "evidence_path": str(evidence)}])
        record = {"task_id": "old", "state": "complete", "result_uploaded": True,
                  "result_digest": hashlib.sha256(evidence.read_bytes()).hexdigest(), "evidence_path": str(evidence)}
        self.assertTrue(migration.drained([record]))
        self.assertTrue(migration.drained([{**record, "state": "failed"}]))
        evidence.write_text("changed bytes")
        with self.assertRaisesRegex(ValueError, "changed"):
            migration.drained([record])

    def test_migration_cancelled_old_task_needs_reason_and_saved_evidence(self):
        evidence = self.root / "cancel.json"
        evidence.write_text('{"reason":"superseded"}')
        record = {"task_id": "old", "state": "cancelled", "evidence_path": str(evidence)}
        with self.assertRaisesRegex(ValueError, "reason"):
            migration.drained([record])
        self.assertTrue(migration.drained([{**record, "reason": "superseded"}]))

    def test_migration_freezes_worker_identity_and_evidence_digest(self):
        state = migration.plan("a" * 40, "b" * 40)
        evidence = self.root / "compatibility.json"
        evidence.write_text(json.dumps({"receiver_accepts_v4": True, "legacy_results_display_only": True, "worker_preflight_ready": True, "worker_revision_sha": "a" * 40}))
        result = migration.advance(state, "compatibility_ready", evidence)
        self.assertEqual(result["next_phase"], "main_ready")
        self.assertEqual(len(result["events"][0]["evidence_sha256"]), 64)
        self.assertFalse(result["production_actions_executed"])

    def test_migration_verifies_upload_and_github_publication_independently(self):
        state = migration.plan("a" * 40, "b" * 40)
        phases = {
            "compatibility_ready": {"receiver_accepts_v4": True, "legacy_results_display_only": True, "worker_preflight_ready": True, "worker_revision_sha": "a" * 40},
            "main_ready": {"minimal_main_dispatch_verified": True, "main_revision_sha": "b" * 40},
            "old_intake_stopped": {"old_intake_stopped": True},
            "old_tasks_drained": {"inventory_complete": True, "tasks": []},
            "poller_ready": {"old_poller_stopped": True, "new_poller_ready": True, "independent_health_ready": True, "external_watchdog_ready": True, "worker_revision_sha": "a" * 40},
        }
        evidence = self.root / "phase.json"
        for phase, payload in phases.items():
            evidence.write_text(json.dumps(payload))
            state = migration.advance(state, phase, evidence)
        result = self.root / "uploaded-result.json"
        result.write_text(json.dumps({"schema": "triton-anchor-local-ci/v4", "task": {"task_id": "c" * 64, "tested_sha": "d" * 40},
                                      "run_id": "fixture-run", "status": "pass"}))
        identity = {"task_id": "c" * 64, "tested_sha": "d" * 40, "run_id": "fixture-run",
                    "result_digest": hashlib.sha256(result.read_bytes()).hexdigest()}
        payload = {"one_way_delivery_verified": True, "immutable_upload_verified": True, "github_publication_verified": True,
                   "cancel_verified": True, "publish_retry_verified": True, "rollback_available": True,
                   "upload": {**identity, "result_uploaded": True, "evidence_path": str(result)},
                   "github_publication": {**identity, "pages_published": True, "comment_published": True, "github_status_published": True}}
        evidence.write_text(json.dumps(payload))
        complete = migration.advance(state, "verified", evidence)
        self.assertIsNone(complete["next_phase"])
        self.assertEqual("one-way", complete["delivery_mode"])
        self.assertFalse(complete["production_actions_executed"])
        for field in ("immutable_upload_verified", "github_publication_verified"):
            evidence.write_text(json.dumps({**payload, field: False}))
            with self.assertRaisesRegex(ValueError, "Required migration evidence"):
                migration.advance(state, "verified", evidence)
        bad_publication = {**payload["github_publication"], "result_digest": "0" * 64}
        evidence.write_text(json.dumps({**payload, "github_publication": bad_publication}))
        with self.assertRaisesRegex(ValueError, "different immutable results"):
            migration.advance(state, "verified", evidence)
        evidence.write_text(json.dumps({**payload, "github_publication": {**payload["github_publication"], "pages_published": False}}))
        with self.assertRaisesRegex(ValueError, "Pages, comment and GitHub status"):
            migration.advance(state, "verified", evidence)

    def test_migration_v1_receipt_plan_cannot_be_silently_reused(self):
        state = migration.plan("a" * 40, "b" * 40)
        state["schema"] = "triton-anchor-local-ci-migration/v1"
        evidence = self.root / "phase.json"
        evidence.write_text("{}")
        with self.assertRaisesRegex(ValueError, "recorded in order"):
            migration.advance(state, "compatibility_ready", evidence)

    def test_one_way_configuration_rejects_receipt_timeout_and_checks_retention(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        self.assertNotIn("receipt_timeout_seconds", config)
        self.assertEqual(30, config["results_retention_days"])
        def check(name):
            result = preflight.check_configuration(config, runtime=False, require_notifications=False)
            return next(row for row in result["checks"] if row["check"] == name)["status"]
        self.assertEqual("pass", check("one_way_delivery"))
        config["receipt_timeout_seconds"] = 60
        self.assertEqual("fail", check("one_way_delivery"))
        del config["receipt_timeout_seconds"]
        for value in (0, -1, True, 1.5, "30"):
            config["results_retention_days"] = value
            self.assertEqual("fail", check("results_retention_days"))
        del config["results_retention_days"]
        self.assertEqual("pass", check("results_retention_days"))

    def test_sessions_cannot_live_inside_trusted_worker_state(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        config["codex_sessions_root"] = config["state_dir"] + "/sessions"
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        check = next(row for row in result["checks"] if row["check"] == "session_state_separation")
        self.assertEqual(check["status"], "fail")


if __name__ == "__main__":
    unittest.main()
