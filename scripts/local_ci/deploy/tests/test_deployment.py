from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        self.assertIn("EnvironmentFile=/", worker)
        self.assertIn("WorkingDirectory=/", worker)
        self.assertIn("WantedBy=default.target", worker)
        self.assertNotIn("multi-user.target", worker)
        self.assertNotIn("Requires=docker.service", worker)
        self.assertNotIn("BindsTo=", worker)
        self.assertNotIn("codex exec", worker)
        for name, text in units.items():
            if name.endswith(".service"):
                self.assertIn("NoNewPrivileges=yes", text)
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

    def test_path_settings_escape_specifiers_without_shell_quotes(self):
        config = {**self.config, "control_root": "/srv/CI files/%instance"}
        units = installer.render_units(config, self.root / "config.json", Path("/srv/CI files/%private.env"))
        worker = units["triton-anchor-local-ci.service"]
        self.assertIn("\nEnvironmentFile=/srv/CI files/%%private.env\n", worker)
        self.assertIn("\nWorkingDirectory=/srv/CI files/%%instance\n", worker)
        for path in ("relative/path", "/tmp/a\nExecStart=bad", "/tmp/a\rvalue", "/tmp/a\x00value", "/tmp/a\\b", "/tmp/*.env", "/tmp/a "):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.unit_path(path)

    def test_ci_user_may_have_manual_sudo_but_not_rootful_runtime_groups(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        account = SimpleNamespace(pw_name="ci_trial", pw_gid=1001)
        for name, expected in (("sudo", "pass"), ("wheel", "pass"), ("admin", "pass"),
                               ("docker", "fail"), ("root", "fail"), ("lxd", "fail"), ("libvirt", "fail")):
            with self.subTest(group=name), patch.object(preflight.pwd, "getpwuid", return_value=account), \
                 patch.object(preflight.grp, "getgrall", return_value=[SimpleNamespace(gr_name=name, gr_gid=999, gr_mem=["ci_trial"])]), \
                 patch.object(preflight.os, "geteuid", return_value=1001), \
                 patch.object(preflight.shutil, "which", return_value=None), \
                 patch.object(preflight, "runtime_status", side_effect=ValueError("No real Docker during test")):
                report = preflight.check_configuration(config, runtime=True, require_notifications=False)
            status = next(row['status'] for row in report['checks'] if row['check'] == 'ci_account_groups')
            self.assertEqual(expected, status)

    def test_sample_fails_missing_production_values(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        self.assertFalse(result["ready"])
        failed = {entry["check"] for entry in result["checks"] if entry["status"] == "fail"}
        self.assertIn("gitee_repo_url", failed)
        self.assertIn("profile:triton_v3.0:image", failed)
        self.assertIn("profile:triton_v3.0:daily_validation", failed)

    def test_smtp_optional_but_gitee_and_health_tokens_required(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        config["health_token_env"] = "CUSTOM_HEALTH_TOKEN"
        for env, expected in (({}, ("pass", "fail", "fail")),
                              ({"GITEE_TOKEN": "fixture", "GITEE_HEALTH_TOKEN": "wrong-key"}, ("pass", "pass", "fail")),
                              ({"GITEE_TOKEN": "fixture", "CUSTOM_HEALTH_TOKEN": "fixture"}, ("pass", "pass", "pass")),
                              ({"LOCAL_CI_SMTP_HOST": "fixture.invalid"}, ("fail", "fail", "fail"))):
            with self.subTest(expected=expected), patch.dict(os.environ, env, clear=True):
                result = preflight.check_configuration(config, runtime=False)
                checks = {item["check"]: item["status"] for item in result["checks"]}
                self.assertEqual(expected, tuple(checks[key] for key in ("smtp", "gitee_publish_auth", "health_publish_auth")))

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

    def test_migration_drains_computation_with_verified_pending_upload(self):
        evidence = self.root / "sealed-result.json"
        evidence.write_text(json.dumps({"schema": "triton-anchor-local-ci/v4", "task": {"task_id": "t1"}, "run_id": "r1", "status": "infra_error"}))
        record = {"task_id": "t1", "state": "publishing", "execution_stopped": True, "result_sealed": True,
                  "evidence_path": str(evidence), "result_digest": hashlib.sha256(evidence.read_bytes()).hexdigest()}
        self.assertTrue(migration.drained([record]))
        for missing in ("execution_stopped", "result_sealed"):
            with self.assertRaisesRegex(ValueError, "stopped execution"):
                migration.drained([{**record, missing: False}])
        with self.assertRaisesRegex(ValueError, "same sealed"):
            migration.drained([{**record, "task_id": "other"}])
        evidence.write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            migration.drained([record])

    def test_migration_freezes_worker_identity_and_evidence_digest(self):
        state = migration.plan("a" * 40, "b" * 40)
        evidence = self.root / "compatibility.json"
        evidence.write_text(json.dumps({"receiver_accepts_v4": True, "legacy_results_display_only": True, "worker_preflight_ready": True, "worker_revision_sha": "a" * 40}))
        result = migration.advance(state, "compatibility_ready", evidence)
        self.assertEqual(result["next_phase"], "rootless_ready")
        self.assertEqual(len(result["events"][0]["evidence_sha256"]), 64)
        self.assertFalse(result["production_actions_executed"])

    def test_migration_verifies_upload_and_github_publication_independently(self):
        state = migration.plan("a" * 40, "b" * 40)
        def saved(name, document):
            path = self.root / name
            path.write_text(json.dumps(document))
            return {"evidence_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        proof = saved("runtime-proof.json", {"schema": "triton-anchor-rootless-runtime-probe/v1", "status": "pass",
                      "runtime": {"uid": 1001, "endpoint": "unix:///run/user/1001/docker.sock"}, "config_digest": "f" * 64,
                      "images": {"main": "sha256:" + "e" * 64}, "resources": {"cpus": 2, "memory_bytes": 100, "pids_limit": 10},
                      "limits": {"main": {"cpu.max": "200000 100000", "memory.max": "100", "pids.max": "10"}}})
        archives = []
        for role in ("control", "state", "workspace", "sessions"):
            path = self.root / (role + ".backup")
            path.write_bytes(b"fixture backup " + role.encode())
            archives.append({"role": role, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        backup = saved("backup.json", {"schema": "triton-anchor-local-ci-runtime-backup/v1", "old_worker_revision_sha": "c" * 40, "files": archives})
        phases = {
            "compatibility_ready": {"receiver_accepts_v4": True, "legacy_results_display_only": True, "worker_preflight_ready": True, "worker_revision_sha": "a" * 40},
            "rootless_ready": {"ordinary_ci_user": True, "user_manager_ready": True, "rootless_verified": True, "resource_limits_verified": True, "runtime_proof": proof},
            "image_releases_ready": {"trusted_images_verified": True, "required_backend_verified": True,
                                     "image_releases": [{"profile": "fixture", "image_id": "sha256:" + "e" * 64, "llvm_hash": "d" * 40, "validated": True}]},
            "main_ready": {"minimal_main_dispatch_verified": True, "main_revision_sha": "b" * 40},
            "old_intake_stopped": {"old_intake_stopped": True},
            "old_tasks_drained": {"inventory_complete": True, "tasks": []},
            "state_migrated": {"terminal_tasks_preserved": True, "pending_uploads_preserved": True, "old_execution_reuse_disabled": True,
                               "runtime_state_separated": True, "old_leases_reconciled": True, "rollback_backup_verified": True, "rollback_backup": backup},
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

    def configured_checks(self, config):
        result = preflight.check_configuration(config, runtime=False, require_notifications=False)
        return {row["check"]: row["status"] for row in result["checks"]}

    def test_workspace_retention_defaults_and_numeric_boundaries(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        defaults = {"task_workspace_retention_hours": 24, "task_workspace_max_bytes": 100 * 1024**3,
                    "cleanup_timeout_seconds": 60}
        for name, expected in defaults.items():
            self.assertEqual(expected, config.pop(name))
        checks = self.configured_checks(config)
        for name in defaults:
            self.assertEqual("pass", checks[name])
        for value in (0, 0.5, 24, 87600):
            with self.subTest(retention=value):
                config["task_workspace_retention_hours"] = value
                self.assertEqual("pass", self.configured_checks(config)["task_workspace_retention_hours"])
        for value in (-1, 87600.5, True, "24", None, float("nan"), float("inf"), 10**400):
            with self.subTest(retention=value):
                config["task_workspace_retention_hours"] = value
                self.assertEqual("fail", self.configured_checks(config)["task_workspace_retention_hours"])
        for name in ("task_workspace_max_bytes", "cleanup_timeout_seconds"):
            for value in (0, -1, True, 1.5, "60", None):
                with self.subTest(field=name, value=value):
                    config[name] = value
                    self.assertEqual("fail", self.configured_checks(config)[name])
            config[name] = 1
            self.assertEqual("pass", self.configured_checks(config)[name])

    def test_branch_profile_mapping_is_checked_before_deployment(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        self.assertEqual(config["branch_profiles"], {"CI_dev": "triton_v3.0"})
        self.assertEqual("pass", self.configured_checks(config)["branch_profiles"])
        for value in (None, [], {"CI_dev": "missing"}, {"CI_dev": []},
                      {"CI_dev_forPR": "triton_v3.0"},
                      {"CI_dev": "alias", "alias": "triton_v3.0"},
                      {"triton_v3.0": "triton_v3.3"}):
            with self.subTest(value=value):
                config["branch_profiles"] = value
                self.assertEqual("fail", self.configured_checks(config)["branch_profiles"])
        del config["branch_profiles"]
        self.assertEqual("pass", self.configured_checks(config)["branch_profiles"])

    def test_execution_identity_rejects_legacy_shared_user_and_duplicate_roles(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        config["runtime"].update(endpoint="unix:///run/user/1001/docker.sock", context="fixture-rootless")
        config["resources"] = {"cpus": 2, "memory_bytes": 100000000, "pids_limit": 64}
        self.assertEqual("pass", self.configured_checks(config)["runtime_resources_identities"])
        config["identities"]["candidate"] = config["identities"]["codex"]
        self.assertEqual("fail", self.configured_checks(config)["runtime_resources_identities"])
        config["profiles"]["triton_v3.0"]["execution_user"] = "0"
        self.assertEqual("fail", self.configured_checks(config)["profile:triton_v3.0:removed_execution_user"])
        config["codex_user"] = "old-host-account"
        self.assertEqual("fail", self.configured_checks(config)["removed_host_codex_user"])

    def test_runtime_preflight_rejects_root_worker(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        account = SimpleNamespace(pw_uid=0, pw_gid=0, pw_name="root")
        with patch.object(preflight.pwd, "getpwuid", return_value=account), \
             patch.object(preflight.grp, "getgrall", return_value=[]), \
             patch.object(preflight.os, "geteuid", return_value=0), \
             patch.object(preflight.shutil, "which", return_value=None):
            result = preflight.check_configuration(config, runtime=True, require_notifications=False)
        row = next(row for row in result["checks"] if row["check"] == "ordinary_ci_user")
        self.assertEqual("fail", row["status"])

    def test_backend_capability_is_mandatory_only_for_triton_30(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        for branch, profile in config["profiles"].items():
            name = f"profile:{branch}:backend"
            required = profile["triton_version"] == "3.0"
            for value in (True, False, None, 1, "true"):
                with self.subTest(branch=branch, value=value):
                    profile["backend_enabled"] = value
                    expected = "pass" if type(value) is bool and value == required else "fail"
                    self.assertEqual(expected, self.configured_checks(config)[name])
            del profile["backend_enabled"]
            self.assertEqual("fail" if required else "pass", self.configured_checks(config)[name])

    def test_preflight_checks_actual_finish_budget_without_host_sessions(self):
        config = json.loads((DEPLOY / "config.example.json").read_text())
        checks = self.configured_checks(config)
        self.assertNotIn("codex_sessions_root", config)
        self.assertNotIn("codex_sessions_root", checks)
        self.assertNotIn("session_state_separation", checks)
        self.assertEqual("pass", checks["finish_timeout_seconds"])
        for value in (839, 0, -1, True, 1.5, "3600", None, 86301):
            with self.subTest(deadline=value):
                config["finish_timeout_seconds"] = value
                self.assertEqual("fail", self.configured_checks(config)["finish_timeout_seconds"])
        config["finish_timeout_seconds"] = 840
        self.assertEqual("pass", self.configured_checks(config)["finish_timeout_seconds"])
        config["management_timeout_seconds"] = 601
        self.assertEqual("fail", self.configured_checks(config)["finish_timeout_seconds"])
        config["finish_timeout_seconds"] = 3600
        config["management_timeout_seconds"] = True
        self.assertEqual("fail", self.configured_checks(config)["finish_timeout_seconds"])

    def test_health_does_not_treat_recipe_roots_as_host_storage(self):
        config = {**self.config, "profiles": {"main": {"workspace_root": "/image/recipe/root"}}}
        manager = SimpleNamespace(health=lambda: {"images": [], "attempts": []})
        result = health.collect(config, manager=manager)
        self.assertEqual(["state"], [entry["label"] for entry in result["storage"]])

    def test_health_preserves_cleanup_failure_and_quarantined_environment(self):
        state = Path(self.config["state_dir"])
        (state / "health").mkdir(parents=True)
        (state / "health/worker.json").write_text(json.dumps({"heartbeat_at": 1000, "pid": os.getpid()}))
        workspace = {"status": "error", "logical_bytes": 101, "max_bytes": 100,
                     "errors": [{"task_id": "fixture", "error": "cleanup_timeout"}],
                     "workspaces": [{"task_id": "fixture", "generation": "g1", "phase": "cleanup_failed"}]}
        (state / "workspace-health.json").write_text(json.dumps(workspace))
        environment = {"active": {}, "generations": [{"generation": "g1", "state": "quarantined",
                       "quarantine_reason": "task_cleanup_failed", "stopped": False, "reusable": False}]}
        manager = SimpleNamespace(health=lambda: environment)
        result = health.collect(self.config, now=1001, manager=manager)
        self.assertEqual("healthy", result["state"])
        self.assertEqual(workspace, result["workspaces"])
        self.assertEqual(environment, result["environments"])

    def test_health_distinguishes_missing_and_unreadable_workspace_snapshot(self):
        state = Path(self.config["state_dir"])
        state.mkdir(parents=True)
        manager = SimpleNamespace(health=lambda: {"active": {}, "generations": []})
        self.assertEqual("unreported", health.collect(self.config, manager=manager)["workspaces"]["status"])
        snapshot = state / "workspace-health.json"
        for content in ("{", "[]", "null"):
            with self.subTest(content=content):
                snapshot.write_text(content)
                result = health.collect(self.config, manager=manager)
                self.assertEqual("error", result["workspaces"]["status"])
                self.assertIn("error", result["workspaces"])
        snapshot.unlink()
        snapshot.mkdir()
        self.assertEqual("error", health.collect(self.config, manager=manager)["workspaces"]["status"])


if __name__ == "__main__":
    unittest.main()
