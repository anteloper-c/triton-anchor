"""Fault contracts for persistent leases, drain, replacement and recovery."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.local_ci.maintenance.workers import (
    WorkerBusy, WorkerError, WorkerManager, atomic_json, file_lock, window_open,
)


class Docker:
    def __init__(self):
        self.containers = {}
        self.calls = []
        self.fail_new_health = False
        self.fail_build = False
        self.fail_llvm_marker = False
        self.offline = False
        self.generation = 0

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        out, code = "", 0
        if self.offline:
            return subprocess.CompletedProcess(argv, 1, "", "unavailable")
        if args[0] == "info":
            out = "27.0"
        elif args[0] == "inspect":
            name = args[-1]
            if name not in self.containers:
                code = 1
            else:
                out = json.dumps([self.containers[name]])
        elif args[0] == "run":
            name = args[args.index("--name") + 1]
            labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
            self.generation += 1
            self.containers[name] = {"Config": {"Labels": labels}, "State": {"Running": True}, "Generation": self.generation}
        elif args[0] == "exec":
            name = args[3] if args[1] == "--user" else args[1]
            if ((self.fail_new_health and self.containers[name]["Generation"] > 1)
                    or (self.fail_llvm_marker and "/usr/bin/python3" in args)):
                code = 1
        elif args[0] == "build":
            code = int(self.fail_build)
        elif args[0] == "stop":
            self.containers[args[-1]]["State"]["Running"] = False
        elif args[0] == "start":
            self.containers[args[-1]]["State"]["Running"] = True
        elif args[0] == "rename":
            self.containers[args[2]] = self.containers.pop(args[1])
        elif args[0] == "rm":
            self.containers.pop(args[-1])
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, code, out, "injected failure" if code else "")


class PersistentWorkers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.profile = {"id": "triton-3.0", "triton_version": "3.0", "llvm_revision": "a" * 40,
                        "container": {"name": "anchor-ci-test", "image": "anchor-ci:test", "healthcheck": ["true"]},
                        "maintenance": {"recipe": {"context": str(self.root)}, "min_free_gb": 0}}
        self.docker = Docker()
        self.manager = WorkerManager(self.root, run=self.docker)

    def test_reuses_one_container_across_tasks_and_manager_restart(self):
        for task in ["task-1", "task-2", "task-3"]:
            self.manager = WorkerManager(self.root, run=self.docker)
            lease = self.manager.acquire(self.profile, task)
            self.assertEqual(lease["container"], "anchor-ci-test")
            self.manager.release(self.profile, task)
        creates = [args for args in self.docker.calls if args[0] == "run"]
        self.assertEqual(len(creates), 1)
        self.assertNotIn("--rm", creates[0])

    def test_busy_lease_is_not_expired_or_overridden_by_force(self):
        self.manager.acquire(self.profile, "task-1")
        state = self.manager._state(self.profile)
        state["lease"]["acquired_at"] = 0
        self.manager._save(self.profile, state)
        self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "waiting")
        with self.assertRaises(WorkerBusy):
            self.manager.acquire(self.profile, "task-2")
        with self.assertRaises(WorkerError):
            self.manager.release(self.profile, "task-2")
        self.assertFalse(any(args[0] in {"stop", "build", "rm"} for args in self.docker.calls))
        self.manager.release(self.profile, "task-1")
        self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "ready")

    def test_unhealthy_replacement_restores_existing_worker(self):
        self.manager.ensure(self.profile)
        original = deepcopy(self.docker.containers["anchor-ci-test"])
        self.docker.fail_new_health = True
        result = self.manager.rebuild(self.profile, force=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.docker.containers, {"anchor-ci-test": original})
        self.assertFalse(self.manager.inspect(self.profile)["draining"])
        self.docker.fail_new_health = False
        self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "ready")
        self.assertIsNone(self.manager.inspect(self.profile)["last_error"])

    def test_failed_build_does_not_stop_old_worker(self):
        self.manager.ensure(self.profile)
        self.docker.fail_build = True
        self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "failed")
        self.assertFalse(any(args[0] == "stop" for args in self.docker.calls))
        self.assertTrue(self.manager.inspect(self.profile)["running"])

    def test_recover_interrupted_rename_before_retrying_failed_build(self):
        self.manager.ensure(self.profile)
        self.docker(["docker", "rename", "anchor-ci-test", "anchor-ci-test-maintenance-rollback"])
        state = self.manager._state(self.profile)
        state["draining"] = True
        state["transaction"] = {"backup": "anchor-ci-test-maintenance-rollback", "had_old": True}
        self.manager._save(self.profile, state)
        self.docker.fail_build = True
        restarted = WorkerManager(self.root, run=self.docker)
        self.assertEqual(restarted.rebuild(self.profile, force=True)["status"], "failed")
        self.assertEqual(list(self.docker.containers), ["anchor-ci-test"])
        self.assertTrue(restarted.inspect(self.profile)["running"])

    def test_llvm_change_requires_trusted_rebuild(self):
        self.manager.ensure(self.profile)
        self.profile["llvm_revision"] = "b" * 40
        with self.assertRaisesRegex(WorkerError, "LLVM profile changed"):
            self.manager.acquire(self.profile, "task-1")
        self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "ready")
        args = next(a for a in self.docker.calls if a[0] == "build")
        self.assertIn("LLVM_REVISION=" + "b" * 40, args)
        self.manager.acquire(self.profile, "task-1")

    def test_prepare_changed_revision_builds_before_replacement(self):
        self.manager.ensure(self.profile)
        self.profile["llvm_revision"] = "b" * 40
        self.assertEqual(self.manager.prepare(self.profile)["status"], "ready")
        calls = self.docker.calls
        self.assertLess(next(i for i, a in enumerate(calls) if a[0] == "build"),
                        next(i for i, a in enumerate(calls) if a[0] == "stop"))
        self.assertEqual(self.docker.containers["anchor-ci-test"]["Config"]["Labels"]
                         ["org.triton-anchor.local-ci.llvm"], "b" * 40)
        count = len([a for a in calls if a[0] == "build"])
        self.manager.prepare(self.profile)
        self.assertEqual(len([a for a in calls if a[0] == "build"]), count)

    def test_prepare_missing_worker_ensures_without_rebuilding(self):
        self.manager.prepare(self.profile)
        self.assertFalse(any(a[0] == "build" for a in self.docker.calls))
        self.assertEqual(len(self.docker.containers), 1)

    def test_prepare_cannot_rebuild_a_lease_or_drain(self):
        self.manager.acquire(self.profile, "active")
        self.profile["llvm_revision"] = "b" * 40
        with self.assertRaises(WorkerBusy):
            self.manager.prepare(self.profile)
        self.manager.release(self.profile, "active")
        state = self.manager._state(self.profile)
        state["draining"] = True
        self.manager._save(self.profile, state)
        with self.assertRaises(WorkerBusy):
            self.manager.prepare(self.profile)
        self.assertFalse(any(a[0] in {"stop", "build"} for a in self.docker.calls))

    def test_prepare_failure_and_shared_build_lock_are_explicit(self):
        self.manager.ensure(self.profile)
        self.profile["llvm_revision"] = "b" * 40
        with file_lock(self.manager.root / "build.lock"):
            with self.assertRaises(WorkerBusy):
                self.manager.prepare(self.profile)
        self.docker.fail_build = True
        with self.assertRaisesRegex(WorkerError, "docker build failed"):
            self.manager.prepare(self.profile)
        del self.profile["maintenance"]["recipe"]
        with self.assertRaisesRegex(WorkerError, "no trusted maintenance recipe"):
            self.manager.prepare(self.profile)

    def test_saved_selection_survives_restart_and_invalidates_on_any_config_change(self):
        selected = deepcopy(self.profile)
        selected["llvm_revision"] = "b" * 40
        selected["llvm_selection"] = {"tested_sha": "c" * 40, "source": "source_blob"}
        self.manager.save_selection(self.profile, selected)
        restarted = WorkerManager(self.root, run=self.docker)
        effective = restarted.effective_profile(self.profile)
        self.assertEqual(effective, selected)
        effective["container"]["name"] = "does-not-mutate-config"
        self.assertEqual(self.profile["container"]["name"], "anchor-ci-test")
        updated = deepcopy(self.profile)
        updated["maintenance"]["min_free_gb"] = 1
        self.assertEqual(restarted.effective_profile(updated), updated)
        updated = deepcopy(self.profile)
        updated["llvm_revision"] = "d" * 40
        self.assertEqual(restarted.effective_profile(updated), updated)

    def test_selection_cannot_alter_recipe_or_container_and_rejects_corrupt_state(self):
        selected = deepcopy(self.profile)
        selected["container"]["image"] = "unexpected:image"
        with self.assertRaises(WorkerError):
            self.manager.save_selection(self.profile, selected)
        self.manager.save_selection(self.profile, self.profile)
        path = self.manager.root / "triton-3.0.selection.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["llvm_revision"] = "untrusted-revision"
        atomic_json(path, saved)
        with self.assertRaisesRegex(WorkerError, "invalid persisted"):
            self.manager.effective_profile(self.profile)

    def test_cli_rebuild_and_ensure_use_saved_selection(self):
        from scripts.local_ci.maintenance.__main__ import main
        config_path = self.root / "config.json"
        atomic_json(config_path, {"state_dir": str(self.root), "profiles": [self.profile]})
        selected = deepcopy(self.profile)
        selected["llvm_revision"] = "b" * 40
        self.manager.save_selection(self.profile, selected)
        with patch("scripts.local_ci.maintenance.__main__.WorkerManager", return_value=self.manager):
            with patch.object(self.manager, "rebuild", return_value={"status": "ready"}) as rebuild:
                self.assertEqual(main(["--config", str(config_path), "rebuild"]), 0)
                self.assertEqual(rebuild.call_args.args[0]["llvm_revision"], "b" * 40)
            with patch.object(self.manager, "ensure", return_value={"status": "ready"}) as ensure:
                self.assertEqual(main(["--config", str(config_path), "ensure"]), 0)
                self.assertEqual(ensure.call_args.args[0]["llvm_revision"], "b" * 40)

    def test_production_health_independently_checks_root_owned_llvm_marker(self):
        self.manager.ensure(self.profile)
        commands = [a for a in self.docker.calls if a[0] == "exec"]
        self.assertEqual(commands[0], ["exec", "anchor-ci-test", "true"])
        marker = commands[1]
        self.assertEqual(marker[:7], ["exec", "--user", "0:0", "anchor-ci-test",
                                     "/usr/bin/python3", "-I", "-c"])
        self.assertIn("/opt/llvm/anchor-ci-llvm-revision", marker[7])
        self.assertIn("st_uid == 0", marker[7])
        self.assertEqual(marker[-1], "a" * 40)
        self.docker.fail_llvm_marker = True
        with self.assertRaises(WorkerError):
            self.manager.acquire(self.profile, "must-not-run")
        self.assertIsNone(self.manager._state(self.profile)["lease"])

    def test_explicit_acceptance_profile_does_not_claim_an_llvm_installation(self):
        self.profile["llvm_revision"] = "acceptance-no-llvm"
        self.manager.ensure(self.profile)
        self.assertEqual([a for a in self.docker.calls if a[0] == "exec"],
                         [["exec", "anchor-ci-test", "true"]])

    def test_low_disk_blocks_rebuild_before_docker_build(self):
        self.profile["maintenance"]["min_free_gb"] = 2
        with patch("scripts.local_ci.maintenance.workers.shutil.disk_usage") as usage:
            usage.return_value.free = 10
            result = self.manager.rebuild(self.profile, force=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("disk", result["error"])
        self.assertFalse(any(args[0] == "build" for args in self.docker.calls))

    def test_unmanaged_same_name_is_not_modified(self):
        self.docker.containers["anchor-ci-test"] = {"Config": {"Labels": {}}, "State": {"Running": False}}
        with self.assertRaisesRegex(WorkerError, "unmanaged"):
            self.manager.ensure(self.profile)
        self.assertFalse(any(args[0] in {"start", "run", "stop", "rm"} for args in self.docker.calls))

    def test_daemon_unavailable_does_not_trigger_create(self):
        self.docker.offline = True
        with self.assertRaises(WorkerError):
            self.manager.ensure(self.profile)
        self.assertFalse(any(args[0] == "run" for args in self.docker.calls))

    def test_no_rm_allowed_in_trusted_config_either(self):
        self.profile["container"]["run_args"] = ["--rm"]
        with self.assertRaisesRegex(WorkerError, "forbidden"):
            self.manager.ensure(self.profile)

    def test_rebuild_global_lock_defers_second_version(self):
        with file_lock(self.manager.root / "build.lock"):
            self.assertEqual(self.manager.rebuild(self.profile, force=True)["status"], "waiting")
        self.assertFalse(any(args[0] == "build" for args in self.docker.calls))

    def test_os_file_lock_excludes_an_independent_process(self):
        lock = self.root / "process.lock"
        script = "from scripts.local_ci.maintenance.workers import file_lock, WorkerBusy\nimport sys\ntry:\n with file_lock(sys.argv[1]): pass\nexcept WorkerBusy:\n sys.exit(23)\n"
        with file_lock(lock):
            result = subprocess.run([sys.executable, "-c", script, str(lock)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        result = subprocess.run([sys.executable, "-c", script, str(lock)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_window_staggers_and_wraps_midnight(self):
        settings = {"window_start": "23:30", "stagger_minutes": 45, "window_minutes": 30}
        self.assertTrue(window_open(settings, datetime(2026, 9, 8, 0, 20, tzinfo=timezone.utc)))
        self.assertFalse(window_open(settings, datetime(2026, 9, 8, 23, 40, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
