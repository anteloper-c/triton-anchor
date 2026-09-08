from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
import fcntl
import sys
from pathlib import Path
import importlib.util

MODULE = Path(__file__).resolve().parents[1] / "manager.py"
spec = importlib.util.spec_from_file_location("environment_manager", MODULE)
manager_module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(manager_module)
EnvironmentManager = manager_module.EnvironmentManager
EnvironmentError = manager_module.EnvironmentError


class FakeDocker:
    """Stateful Docker boundary; manager, registry, archives and git remain real."""
    def __init__(self):
        self.containers = {}
        self.commands = []
        self.fail_validation = False
        self.fail_tool = None
        self.fail_stop = False
        self.device_changes_public_state = False
        self.timeout_tool = None

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] != "docker":
            return subprocess.run(argv, **kwargs)
        action = argv[1]
        output, code = "", 0
        identified_name = next((name for name, item in self.containers.items() if item["Id"] == argv[-1]), argv[-1])
        if action == "create":
            name = argv[argv.index("--name") + 1]
            labels, mounts = {}, []
            for index, value in enumerate(argv):
                if value == "--label":
                    key, entry = argv[index + 1].split("=", 1)
                    labels[key] = entry
                if value == "--mount":
                    fields = dict(part.split("=", 1) for part in argv[index + 1].split(",") if "=" in part)
                    mounts.append({"Source": fields["source"], "Destination": fields["target"]})
            self.containers[name] = {"Id": "fake-" + name, "Image": "sha256:fixture", "Config": {"Labels": labels}, "Mounts": mounts, "State": {"Running": False}, "Hygiene": {}}
        elif action == "start":
            self.containers[argv[-1]]["State"]["Running"] = True
        elif action == "stop":
            if self.fail_stop:
                code = 1
            else:
                self.containers[identified_name]["State"]["Running"] = False
        elif action == "inspect":
            if identified_name not in self.containers:
                code = 1
            else:
                output = json.dumps([self.containers[identified_name]])
        elif action == "exec":
            index = 2
            while argv[index].startswith("--"):
                index += 2
            name = argv[index]
            command = argv[index + 1:]
            container = self.containers[name]
            mount = container["Mounts"][0]
            if manager_module.HYGIENE_SNAPSHOT in command:
                output = json.dumps({path: container["Hygiene"].get(path, "0" * 64) for path in json.loads(command[-1])})
            elif command[0] == "test":
                mapped = next((entry for entry in container["Mounts"] if command[2].startswith(entry["Destination"] + "/") or command[2] == entry["Destination"]), mount)
                host = Path(command[2].replace(mapped["Destination"], mapped["Source"], 1))
                code = 0 if (host.is_dir() if command[1] == "-x" else host.is_file()) else 1
            elif command[0] == "cmake" and "--build" in command:
                workspace = Path(mount["Source"])
                revision = [arg.split("=", 1)[1] for arg in argv if arg.startswith("LOCAL_CI_LLVM_HASH=")][0]
                install = workspace / "deps" / f"llvm-{revision}"
                (install / "lib/cmake/mlir").mkdir(parents=True)
                (install / "lib/cmake/mlir/MLIRConfig.cmake").write_text("fixture")
                (install / "bin").mkdir()
                (install / "bin/llvm-config").write_text("fixture")
                (install / "bin/mlir-opt").write_text("fixture")
            elif command[0] == "validate":
                if command[-1] == self.timeout_tool:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                if self.fail_validation or command[-1] == self.fail_tool:
                    code = 19
                elif command[-1] == "device-clean" and self.device_changes_public_state:
                    container["Hygiene"]["/tmp"] = "1" * 64
            elif command[0] == "id":
                output = "10001\n"
            elif command[-1] == "--version":
                output = "fixture LLVM\n"
        elif action == "rm":
            self.containers.pop(argv[-1], None)
        else:
            raise AssertionError(f"Unexpected Docker operation: {argv}")
        return subprocess.CompletedProcess(argv, code, output, "fixture failure" if code else "")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        control = self.root / "control"
        (control / "scripts/local_ci/tools").mkdir(parents=True)
        (control / "scripts/local_ci/tools/fixture").write_text("fixture")
        (control / "scripts/local_ci/tools/run_tool.py").write_text("# fixture trusted tool")
        subprocess.run(["git", "init", "-q", str(control)], check=True)
        subprocess.run(["git", "-C", str(control), "add", "."], check=True)
        subprocess.run(["git", "-C", str(control), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "control fixture"], check=True)
        self.sha = "a" * 40
        self.archive = self.root / "llvm.tar.gz"
        with tarfile.open(self.archive, "w:gz") as handle:
            for name in ("bin/llvm-config", "bin/mlir-opt", "lib/cmake/mlir/MLIRConfig.cmake"):
                data = b"fixture"
                member = tarfile.TarInfo("llvm/" + name)
                member.size = len(data)
                member.mode = 0o755
                handle.addfile(member, io.BytesIO(data))
        self.config = {"control_root": str(control), "minimum_free_bytes": 0, "profiles": {
            "triton_v3.0": {"name": "triton-3.0", "triton_version": "3.0", "llvm_hash": self.sha,
                "backend_enabled": True, "image": "fixture-image", "execution_user": "10001:10001", "workspace_root": str(self.root / "workspaces"),
                "env": {"PYTHON_VENV_ACTIVATE": "/opt/venv/bin/activate"},
                "post_task_validation_commands": [["validate", "device-clean"]],
                "llvm": {"mode": "archive", "archive": str(self.archive), "sha256": manager_module.file_digest(self.archive), "commit": self.sha},
                "validation_commands": {name: ["validate", name] for name in ("environment", "frontend_build", "wheel_install_import", "frontend_smoke", "backend_rebuild", "backend_smoke_jit")}}}}
        self.docker = FakeDocker()
        self.manager = EnvironmentManager(self.config, self.root / "state", self.docker)

    def test_tasks_share_persistent_generation(self):
        first = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        second = self.manager.acquire("task-b", "triton_v3.0", self.sha)
        self.assertEqual(first["container"], second["container"])
        self.assertEqual(len(self.docker.containers), 1)
        self.manager.release("task-a")
        self.assertIn("task-b", self.manager.health()["leases"])
        self.assertFalse(any(command[1] in {"run", "commit", "rm"} for command in self.docker.commands if command[0] == "docker"))

    def test_private_service_umask_keeps_dependencies_readable_without_write_access(self):
        old_mask = os.umask(0o077)
        try:
            generation = self.manager.acquire("task-private-umask", "triton_v3.0", self.sha)
        finally:
            os.umask(old_mask)
        workspace = Path(generation["workspace_host"])
        self.assertEqual(workspace.stat().st_mode & 0o077, 0o055)
        dependency = workspace / "deps" / ("llvm-" + self.sha)
        for path in (dependency, dependency / "bin", dependency / "bin/llvm-config"):
            self.assertEqual(path.stat().st_mode & 0o055, 0o055)
            self.assertEqual(path.stat().st_mode & 0o022, 0)
        self.assertTrue(any("import build, setuptools, wheel, pybind11, yaml, pytest" in command for command in self.docker.commands))

    def test_rotation_keeps_leased_old_generation(self):
        old = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        new = self.manager.rotate("triton_v3.0")
        self.assertNotEqual(old["generation"], new["generation"])
        self.assertEqual(self.manager.acquire("task-a", "triton_v3.0", self.sha)["generation"], old["generation"])
        self.assertEqual(self.manager.acquire("task-b", "triton_v3.0", self.sha)["generation"], new["generation"])
        self.assertTrue(self.docker.containers[old["container"]]["State"]["Running"])

    def test_failed_daily_validation_does_not_promote(self):
        old = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        self.docker.fail_validation = True
        with self.assertRaises(EnvironmentError):
            self.manager.rotate("triton_v3.0")
        self.assertEqual(self.manager.health()["active"]["triton_v3.0"], old["generation"])
        self.assertTrue(any(row["state"] == "failed" for row in self.manager.health()["generations"]))

    def test_rollback_retains_new_task_lease(self):
        old = self.manager.ensure("triton_v3.0", self.sha)
        new = self.manager.rotate("triton_v3.0")
        self.manager.acquire("task-new", "triton_v3.0", self.sha)
        self.assertEqual(self.manager.rollback("triton_v3.0")["generation"], old["generation"])
        self.assertEqual(self.manager.acquire("task-new", "triton_v3.0", self.sha)["generation"], new["generation"])

    def test_restart_reads_lease_instead_of_recreating(self):
        old = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        resumed = EnvironmentManager(self.config, self.root / "state", self.docker)
        self.assertEqual(resumed.acquire("task-a", "triton_v3.0", self.sha)["container"], old["container"])
        self.assertEqual(len(self.docker.containers), 1)

    def test_hash_change_does_not_fallback_or_disable_backend(self):
        self.manager.ensure("triton_v3.0", self.sha)
        with self.assertRaisesRegex(EnvironmentError, "New LLVM revision"):
            self.manager.acquire("upgrade", "triton_v3.0", "b" * 40)
        self.assertTrue(self.config["profiles"]["triton_v3.0"]["backend_enabled"])

    def test_new_archive_hash_keeps_backend_capability(self):
        new_sha = "b" * 40
        self.config["profiles"]["triton_v3.0"]["llvm"]["revisions"] = {new_sha: {"commit": new_sha}}
        result = self.manager.acquire("upgrade", "triton_v3.0", new_sha)
        self.assertTrue(result["backend_enabled"])
        self.assertEqual(result["llvm_hash"], new_sha)
        self.assertNotIn("triton_v3.0", self.manager.health()["active"])

    def test_changed_container_identity_is_rejected(self):
        old = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.containers[old["container"]]["Id"] = "replacement"
        with self.assertRaisesRegex(EnvironmentError, "identity changed"):
            self.manager.acquire("task", "triton_v3.0", self.sha)

    def test_corrupt_registry_fails_closed(self):
        self.manager.registry.write_text("broken")
        with self.assertRaisesRegex(EnvironmentError, "registry is unreadable"):
            self.manager.ensure("triton_v3.0", self.sha)

    def test_bad_archive_checksum_is_rejected(self):
        self.config["profiles"]["triton_v3.0"]["llvm"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(EnvironmentError, "SHA256"):
            self.manager.ensure("triton_v3.0", self.sha)

    def test_archive_traversal_is_rejected(self):
        archive = self.root / "evil.tar"
        with tarfile.open(archive, "w") as handle:
            member = tarfile.TarInfo("llvm/../../escape")
            member.size = 1
            handle.addfile(member, io.BytesIO(b"x"))
        with self.assertRaises(EnvironmentError):
            manager_module.extract_verified_archive(archive, self.root / "extract", manager_module.file_digest(archive))
        self.assertFalse((self.root / "escape").exists())

    def test_frontend_only_never_requires_backend_validation(self):
        recipe = self.config["profiles"]["triton_v3.0"]
        recipe.update(triton_version="3.3", backend_enabled=False)
        recipe["validation_commands"].pop("backend_rebuild")
        recipe["validation_commands"].pop("backend_smoke_jit")
        result = self.manager.rotate("triton_v3.0")
        self.assertFalse(result["backend_enabled"])
        self.assertEqual(result["env"]["RUN_BACKEND_STAGES"], "false")

    def test_non_30_profile_cannot_inherit_backend(self):
        self.config["profiles"]["triton_v3.0"]["triton_version"] = "3.6"
        with self.assertRaisesRegex(EnvironmentError, "Only the deployed Triton 3.0"):
            self.manager.ensure("triton_v3.0", self.sha)

    def test_missing_daily_validations_are_not_success(self):
        self.config["profiles"]["triton_v3.0"]["validation_commands"].pop("backend_rebuild")
        with self.assertRaisesRegex(EnvironmentError, "lacks required"):
            self.manager.rotate("triton_v3.0")

    def test_local_source_mirror_exact_commit(self):
        source = self.root / "llvm-source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "llvm").mkdir()
        (source / "llvm/CMakeLists.txt").write_text("# fixture")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        self.config["profiles"]["triton_v3.0"]["llvm"] = {"mode": "source", "repository": str(source)}
        result = self.manager.ensure("triton_v3.0", revision)
        self.assertEqual(result["llvm_hash"], revision)
        self.assertTrue((Path(result["workspace_host"]) / "deps" / f"llvm-{revision}/local-ci-provenance.json").is_file())

    def test_github_is_not_a_server_source(self):
        with self.assertRaises(EnvironmentError):
            manager_module.safe_source("https://github.com/example/project.git", "source")

    def test_llvm_cache_is_verified_and_reused_between_generations(self):
        first = self.manager.ensure("triton_v3.0", self.sha)
        cache = next((self.manager.directory / "llvm-cache").glob("*/ready.json"))
        marker = json.loads(cache.read_text())
        self.assertEqual(marker["llvm_hash"], self.sha)
        second = self.manager.rotate("triton_v3.0")
        self.assertEqual(len(list((self.manager.directory / "llvm-cache").glob("*/ready.json"))), 1)
        self.assertNotEqual(first["workspace_host"], second["workspace_host"])

    def test_corrupt_llvm_cache_is_quarantined_and_rebuilt(self):
        self.manager.ensure("triton_v3.0", self.sha)
        cached = next((self.manager.directory / "llvm-cache").glob("*/install/bin/llvm-config"))
        cached.write_text("corruption")
        self.manager.rotate("triton_v3.0")
        self.assertEqual(len(list((self.manager.directory / "llvm-cache").glob("*.invalid-*"))), 1)

    def test_retention_preserves_lease_and_previous_generation(self):
        self.config["generation_retention_hours"] = 0
        oldest = self.manager.acquire("running", "triton_v3.0", self.sha)
        previous = self.manager.rotate("triton_v3.0")
        newest = self.manager.rotate("triton_v3.0")
        self.assertFalse(self.manager.collect_retired()["removed"])
        self.manager.release("running")
        collected = self.manager.collect_retired()
        self.assertEqual(collected["removed"], [oldest["generation"]])
        self.assertIn(previous["container"], self.docker.containers)
        self.assertIn(newest["container"], self.docker.containers)

    def test_real_subprocess_is_cancelled(self):
        manager = EnvironmentManager(self.config, self.root / "cancel-state")
        manager.cancel_event = threading.Event()
        timer = threading.Timer(0.1, manager.cancel_event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaisesRegex(EnvironmentError, "cancelled"):
            manager._run([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertLess(time.monotonic() - started, 3)

    def test_cancellation_interrupts_wait_for_environment_lock(self):
        self.manager.cancel_event = threading.Event()
        with (self.manager.state_dir / "resource.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            timer = threading.Timer(0.1, self.manager.cancel_event.set)
            timer.start()
            self.addCleanup(timer.cancel)
            with self.assertRaisesRegex(EnvironmentError, "cancelled while waiting"):
                self.manager.acquire("cancelled", "triton_v3.0", self.sha)
        self.assertFalse(self.docker.containers)

    def test_internal_llvm_library_symlink_is_supported(self):
        archive = self.root / "linked.tar"
        with tarfile.open(archive, "w") as handle:
            member = tarfile.TarInfo("llvm/lib/libLLVM-19.so")
            member.size = 1
            handle.addfile(member, io.BytesIO(b"x"))
            link = tarfile.TarInfo("llvm/lib/libLLVM.so")
            link.type, link.linkname = tarfile.SYMTYPE, "libLLVM-19.so"
            handle.addfile(link)
        target = self.root / "linked"
        manager_module.extract_verified_archive(archive, target, manager_module.file_digest(archive))
        self.assertEqual((target / "lib/libLLVM.so").read_bytes(), b"x")

    def test_control_revision_changes_environment_fingerprint(self):
        first = self.manager.ensure("triton_v3.0", self.sha)
        control = Path(self.config["control_root"])
        (control / "scripts/local_ci/tools/fixture").write_text("new control")
        subprocess.run(["git", "-C", str(control), "add", "."], check=True)
        subprocess.run(["git", "-C", str(control), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "control change"], check=True)
        second = self.manager.ensure("triton_v3.0", self.sha)
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertNotEqual(first["environment_fingerprint"], second["environment_fingerprint"])

    def test_new_llvm_backend_failure_blocks_promotion(self):
        old = self.manager.ensure("triton_v3.0", self.sha)
        recipe = self.config["profiles"]["triton_v3.0"]
        recipe["llvm_hash"] = "b" * 40
        recipe["llvm"]["commit"] = "b" * 40
        self.docker.fail_tool = "backend_rebuild"
        with self.assertRaises(EnvironmentError):
            self.manager.rotate("triton_v3.0")
        snapshot = self.manager.health()
        self.assertEqual(snapshot["active"]["triton_v3.0"], old["generation"])
        candidate = list(self.manager._load()["generations"].values())[-1]
        self.assertTrue(candidate["backend_enabled"])
        self.assertEqual(candidate["state"], "failed")
        self.assertFalse(self.docker.containers[candidate["container"]]["State"]["Running"])

    def test_dirty_restart_preserves_active_role_and_task_files_do_not_change_baseline(self):
        generation = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        self.manager.mark_dirty(generation["generation"], "task-a")
        tasks = Path(generation["workspace_host"]) / "tasks/task-a"
        tasks.mkdir(parents=True)
        (tasks / "untrusted-output").write_text("task-only changes")
        restarted = EnvironmentManager(self.config, self.root / "state", self.docker)
        row = restarted.health()["generations"][0]
        self.assertTrue(row["active"])
        self.assertEqual(row["state"], "dirty")
        self.assertFalse(row["reusable"])
        self.assertEqual(restarted.leases()["task-a"]["generation"], generation["generation"])
        self.assertEqual(restarted.generations()[generation["generation"]]["execution_uid"], 10001)
        changed_snapshot = restarted.generations()
        changed_snapshot[generation["generation"]]["state"] = "active"
        self.assertEqual(restarted.generation(generation["generation"])["state"], "dirty")
        with self.assertRaisesRegex(EnvironmentError, "dirty"):
            restarted.acquire("task-a", "triton_v3.0", self.sha)
        self.assertEqual(restarted.validate_reuse(generation["generation"])["state"], "active")
        restarted.release("task-a")
        self.assertEqual(restarted.acquire("task-b", "triton_v3.0", self.sha)["generation"], generation["generation"])

    def test_shared_content_change_quarantines_then_replaces_without_deleting_diagnostics(self):
        generation = self.manager.acquire("task-a", "triton_v3.0", self.sha)
        self.manager.mark_dirty(generation["generation"], "task-a")
        dependency = Path(generation["workspace_host"]) / f"deps/llvm-{self.sha}/bin/llvm-config"
        dependency.write_text("contamination")
        with self.assertRaisesRegex(EnvironmentError, "quarantined"):
            self.manager.validate_reuse(generation["generation"])
        isolated = self.manager.generation(generation["generation"])
        self.assertEqual(isolated["quarantine_reason"], "shared_dependency_or_public_state_changed")
        self.assertTrue(isolated["stopped"])
        self.assertEqual(dependency.read_text(), "contamination")
        self.assertIn("task-a", self.manager.leases())
        self.assertNotIn("triton_v3.0", self.manager.health()["active"])
        self.manager.release("task-a")
        replacement = self.manager.acquire("task-b", "triton_v3.0", self.sha)
        self.assertNotEqual(replacement["generation"], generation["generation"])
        self.assertEqual((Path(replacement["workspace_host"]) / f"deps/llvm-{self.sha}/bin/llvm-config").read_text(), "fixture")

    def test_dependency_permission_and_owner_are_part_of_baseline(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        dependency = Path(generation["workspace_host"]) / "deps"
        baseline = manager_module.shared_workspace_digest(Path(generation["workspace_host"]))
        dependency.chmod(0o777)
        self.assertNotEqual(manager_module.shared_workspace_digest(Path(generation["workspace_host"])), baseline)
        with self.assertRaisesRegex(EnvironmentError, "quarantined"):
            self.manager.validate_reuse(generation["generation"])
        if os.geteuid() == 0:
            dependency.chmod(0o755)
            os.chown(dependency, 12345, 12345)
            self.assertNotEqual(manager_module.shared_workspace_digest(Path(generation["workspace_host"])), baseline)

    def test_public_residue_is_retained_and_each_public_root_is_checked(self):
        for number, path in enumerate(manager_module.PUBLIC_PATHS):
            with self.subTest(path=path):
                task_id = f"task-{number}"
                generation = self.manager.acquire(task_id, "triton_v3.0", self.sha)
                self.manager.mark_dirty(generation["generation"], task_id)
                self.docker.containers[generation["container"]]["Hygiene"][path] = "1" * 64
                with self.assertRaisesRegex(EnvironmentError, "quarantined"):
                    self.manager.validate_reuse(generation["generation"])
                self.assertEqual(self.docker.containers[generation["container"]]["Hygiene"][path], "1" * 64)
                self.assertFalse(any(command[:2] == ["docker", "rm"] for command in self.docker.commands))
                self.manager.release(task_id)

    def test_post_task_device_failure_and_timeout_quarantine(self):
        for attribute in ("fail_tool", "timeout_tool"):
            with self.subTest(attribute=attribute):
                generation = self.manager.acquire(attribute, "triton_v3.0", self.sha)
                self.manager.mark_dirty(generation["generation"], attribute)
                setattr(self.docker, attribute, "device-clean")
                with self.assertRaisesRegex(EnvironmentError, "quarantined"):
                    self.manager.validate_reuse(generation["generation"])
                self.assertEqual(self.manager.generation(generation["generation"])["quarantine_reason"], "post_task_device_validation_failed")
                self.assertTrue(self.manager.generation(generation["generation"])["stopped"])
                self.manager.release(attribute)
                setattr(self.docker, attribute, None)

    def test_post_task_probe_cannot_leave_new_public_residue(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.device_changes_public_state = True
        with self.assertRaisesRegex(EnvironmentError, "quarantined"):
            self.manager.validate_reuse(generation["generation"])
        self.assertEqual(self.manager.generation(generation["generation"])["quarantine_reason"], "post_validation_changed_shared_or_public_state")

    def test_unconfirmed_container_stop_blocks_all_selection_until_retry_confirms(self):
        previous = self.manager.ensure("triton_v3.0", self.sha)
        generation = self.manager.rotate("triton_v3.0")
        self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.fail_stop = True
        isolated = self.manager.quarantine(generation["generation"], "task_process_stop_failed")
        self.assertFalse(isolated["stopped"])
        restarted = EnvironmentManager(self.config, self.root / "state", self.docker)
        for action in (lambda: restarted.acquire("task", "triton_v3.0", self.sha),
                       lambda: restarted.acquire("next", "triton_v3.0", self.sha),
                       lambda: restarted.ensure("triton_v3.0", self.sha),
                       lambda: restarted.rotate("triton_v3.0"), lambda: restarted.rollback("triton_v3.0")):
            with self.assertRaisesRegex(EnvironmentError, "stop is unconfirmed"):
                action()
        self.assertEqual(restarted.leases()["task"]["generation"], generation["generation"])
        self.docker.fail_stop = False
        self.assertTrue(restarted.quarantine(generation["generation"], "task_process_stop_failed")["stopped"])
        restarted.release("task")
        self.assertEqual(restarted.ensure("triton_v3.0", self.sha)["generation"], previous["generation"])

    def test_quarantine_never_stops_a_replaced_container(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.containers[generation["container"]]["Id"] = "different-container"
        isolated = self.manager.quarantine(generation["generation"], "container_identity_mismatch")
        self.assertFalse(isolated["stopped"])
        self.assertTrue(self.docker.containers[generation["container"]]["State"]["Running"])
        self.assertFalse(any(command[:2] == ["docker", "stop"] for command in self.docker.commands))

    def test_dirty_previous_cannot_rollback_until_validated(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.manager.mark_dirty(generation["generation"], "task")
        self.manager.rotate("triton_v3.0")
        self.assertEqual(self.manager.generation(generation["generation"])["reuse_role"], "previous")
        with self.assertRaisesRegex(EnvironmentError, "No retained previous"):
            self.manager.rollback("triton_v3.0")
        self.assertEqual(self.manager.validate_reuse(generation["generation"])["state"], "previous")
        self.assertEqual(self.manager.rollback("triton_v3.0")["generation"], generation["generation"])

    def test_legacy_generation_without_baseline_is_stopped_and_replaced(self):
        generation = self.manager.ensure("triton_v3.0", self.sha)
        state = self.manager._load()
        del state["generations"][generation["generation"]]["reuse_baseline"]
        self.manager._save(state)
        replacement = self.manager.ensure("triton_v3.0", self.sha)
        self.assertNotEqual(replacement["generation"], generation["generation"])
        self.assertEqual(self.manager.generation(generation["generation"])["state"], "quarantined")
        self.assertFalse(self.docker.containers[generation["container"]]["State"]["Running"])

    def test_quarantine_retention_waits_for_stop_and_lease_release(self):
        self.config["generation_retention_hours"] = 0
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.fail_stop = True
        self.manager.quarantine(generation["generation"], "cleanup_failed")
        self.assertFalse(self.manager.collect_retired()["removed"])
        self.docker.fail_stop = False
        self.manager.quarantine(generation["generation"], "cleanup_failed")
        self.assertFalse(self.manager.collect_retired()["removed"])
        self.manager.release("task")
        self.assertEqual(self.manager.collect_retired()["removed"], [generation["generation"]])
        self.assertFalse(Path(generation["workspace_host"]).exists())

    def test_quarantine_retry_preserves_first_quarantine_time(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.docker.fail_stop = True
        with mock.patch.object(manager_module, "utc_now", return_value="2020-01-01T00:00:00Z"):
            first = self.manager.quarantine(generation["generation"], "cleanup_failed")
        self.docker.fail_stop = False
        second = self.manager.quarantine(generation["generation"], "cleanup_failed")
        self.assertTrue(second["stopped"])
        self.assertEqual(second["quarantined_at"], first["quarantined_at"])

    def test_workspace_digest_deadline_interrupts_directory_walk(self):
        root = self.root / "fingerprint-fixture"
        (root / "child").mkdir(parents=True)
        with mock.patch.object(manager_module.time, "monotonic", side_effect=[0, 0, 0.5, 1.1]):
            with self.assertRaisesRegex(EnvironmentError, "fingerprint timed out"):
                manager_module.shared_workspace_digest(root, timeout_seconds=1)

    def test_workspace_digest_deadline_interrupts_large_file_between_chunks(self):
        root = self.root / "fingerprint-fixture"
        root.mkdir()
        payload = root / "large-library"
        payload.write_bytes(b"fixture")
        clock = [0.0]
        positions = []
        class SlowSource(io.BytesIO):
            def read(self, size=-1):
                clock[0] += 0.6
                result = super().read(size)
                positions.append(self.tell())
                return result
        data = b"x" * (4 * 1024 * 1024)
        with mock.patch.object(Path, "open", return_value=SlowSource(data)), mock.patch.object(manager_module.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(EnvironmentError, "fingerprint timed out"):
                manager_module.shared_workspace_digest(root, timeout_seconds=1)
        self.assertEqual(positions, [1024 * 1024, 2 * 1024 * 1024])

    def test_reuse_uses_configured_host_deadline_and_matches_prepared_baseline(self):
        self.config["hygiene_snapshot_timeout_seconds"] = 60
        with mock.patch.object(manager_module, "shared_workspace_digest", wraps=manager_module.shared_workspace_digest) as digest:
            generation = self.manager.acquire("task", "triton_v3.0", self.sha)
            self.manager.mark_dirty(generation["generation"], "task")
            checked = self.manager.validate_reuse(generation["generation"])
        self.assertEqual(checked["reuse_baseline"], generation["reuse_baseline"])
        self.assertEqual(digest.call_count, 3)
        self.assertTrue(all(call.kwargs == {"timeout_seconds": 60} for call in digest.call_args_list))

    def test_runtime_workspace_deadline_failure_quarantines(self):
        generation = self.manager.acquire("task", "triton_v3.0", self.sha)
        self.manager.mark_dirty(generation["generation"], "task")
        with mock.patch.object(manager_module, "shared_workspace_digest", side_effect=EnvironmentError("Shared dependency fingerprint timed out")):
            with self.assertRaisesRegex(EnvironmentError, "quarantined"):
                self.manager.validate_reuse(generation["generation"])
        self.assertEqual(self.manager.generation(generation["generation"])["state"], "quarantined")
        self.assertTrue(self.manager.generation(generation["generation"])["stopped"])

    def test_device_recipe_defaults_are_real_and_identity_numeric(self):
        recipe = self.config["profiles"]["triton_v3.0"]
        self.assertEqual(manager_module.EnvironmentManager._hygiene_recipe(recipe)[1], 120)
        for commands in ([], [["true"]], [["/bin/true"]], ["device-status"]):
            recipe["post_task_validation_commands"] = commands
            with self.assertRaisesRegex(EnvironmentError, "real post_task_validation"):
                self.manager.ensure("triton_v3.0", self.sha)
        recipe["backend_enabled"] = False
        with self.assertRaisesRegex(EnvironmentError, "real post_task_validation"):
            self.manager.ensure("triton_v3.0", self.sha)
        recipe["triton_version"] = "3.1"
        recipe["post_task_validation_commands"] = []
        recipe["execution_user"] = "named-user"
        with self.assertRaisesRegex(EnvironmentError, "numeric execution_user"):
            self.manager.ensure("triton_v3.0", self.sha)

    def test_pid_one_is_root_and_python_probes_do_not_create_bytecode(self):
        self.manager.ensure("triton_v3.0", self.sha)
        create = next(command for command in self.docker.commands if command[:2] == ["docker", "create"])
        self.assertEqual(create[create.index("--user") + 1], "0:0")
        probes = [command for command in self.docker.commands if "import build, setuptools, wheel, pybind11, yaml, pytest" in command]
        self.assertTrue(all("PYTHONDONTWRITEBYTECODE=1" in command for command in probes))

    def test_real_public_snapshot_covers_contents_and_modes_without_leaking_contents(self):
        directory = self.root / "public-fixture"
        directory.mkdir()
        payload = directory / "private-name"
        payload.write_text("must-not-appear-in-output")
        def snapshot():
            return subprocess.check_output([sys.executable, "-I", "-S", "-B", "-c", manager_module.HYGIENE_SNAPSHOT, json.dumps([str(directory)])], text=True)
        original = snapshot()
        self.assertNotIn("private-name", original)
        self.assertNotIn("must-not-appear", original)
        payload.chmod(0o777)
        changed_mode = snapshot()
        self.assertNotEqual(original, changed_mode)
        payload.write_text("different content")
        self.assertNotEqual(changed_mode, snapshot())


if __name__ == "__main__":
    unittest.main()
