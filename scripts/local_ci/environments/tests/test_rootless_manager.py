"""Real registry/Git/archive operations with a stateful Docker protocol fixture."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from environments import runtime
from environments.artifacts import tree_digest
from environments.dependency_mounts import dependency_mounts
from environments.manager import (
    EnvironmentManager,
    EnvironmentError,
    docker_command,
    identities,
    file_digest,
    extract_verified_archive,
)


class FakeDocker:
    """Docker daemon boundary only; never invokes a Docker binary."""

    def __init__(self):
        self.commands, self.inputs = [], []
        self.containers, self.images, self.volumes = {}, {}, {}
        self.rootless, self.daemon = True, "fixture-daemon"
        self.fail_check, self.fail_stop, self.fail_volume_rm = None, False, False
        self.fail_create = False

    def __call__(self, command, **kwargs):
        if command[0] != "docker":
            return subprocess.run(command, **kwargs)
        self.commands.append(command)
        self.inputs.append(kwargs.get("input"))
        assert command[1:3] == ["--host", "unix:///run/user/1000/docker.sock"]
        args = command[3:]
        output, code = b"", 0
        labels = dict(
            args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label"
        )
        try:
            if args[0] == "info":
                output = json.dumps(
                    {
                        "ID": self.daemon,
                        "SecurityOptions": ["name=rootless"] if self.rootless else [],
                        "CgroupVersion": "2",
                        "CgroupDriver": "systemd",
                    }
                )
            elif args[0] == "build":
                image = (
                    "sha256:"
                    + hashlib.sha256(str(len(self.images)).encode()).hexdigest()
                )
                Path(args[args.index("--iidfile") + 1]).write_text(image)
                self.images[image] = {"Id": image, "Config": {"Labels": labels}}
            elif args[0] == "load":
                pass
            elif args[:2] == ["image", "inspect"]:
                output = json.dumps([self.images[args[-1]]])
            elif args[:2] == ["image", "rm"]:
                del self.images[args[-1]]
            elif args[:2] == ["volume", "create"]:
                self.volumes[args[-1]] = {"Name": args[-1], "Labels": labels}
                output = args[-1]
            elif args[:2] == ["volume", "inspect"]:
                output = json.dumps([self.volumes[args[-1]]])
            elif args[:2] == ["volume", "ls"]:
                output = "\n".join(self.volumes)
            elif args[:2] == ["volume", "rm"]:
                if self.fail_volume_rm:
                    code = 1
                else:
                    del self.volumes[args[-1]]
            elif args[0] == "create":
                if self.fail_create:
                    code = 1
                else:
                    ident = "cid-" + str(len(self.commands))
                    mounts = []
                    for i, arg in enumerate(args):
                        if arg != "--mount":
                            continue
                        raw = args[i + 1]
                        fields = dict(
                            part.split("=", 1) for part in raw.split(",") if "=" in part
                        )
                        mounts.append(
                            {
                                "Type": fields["type"],
                                "Source": fields["source"],
                                "Name": fields["source"],
                                "Destination": fields["target"],
                                "RW": ",readonly" not in raw,
                            }
                        )
                    self.containers[ident] = {
                        "Id": ident,
                        "Name": args[args.index("--name") + 1],
                        "Image": args[-1],
                        "Config": {"Labels": labels},
                        "Mounts": mounts,
                        "State": {"Running": False},
                    }
                    output = ident
            elif args[0] == "inspect":
                output = json.dumps([self.containers[args[-1]]])
            elif args[0] in {"start", "stop"}:
                if args[0] == "stop" and self.fail_stop:
                    code = 1
                else:
                    self.containers[args[-1]]["State"]["Running"] = args[0] == "start"
            elif args[0] == "rm":
                del self.containers[args[-1]]
            elif args[0] == "ps":
                ids = list(self.containers)
                if "--filter" in args:
                    wanted = args[args.index("--filter") + 1][7:-1]
                    ids = [key for key in ids if self.containers[key]["Name"] == wanted]
                output = "\n".join(ids)
            elif args[0] == "exec":
                if self.fail_check and self.fail_check in args:
                    code = 1
                elif runtime.HELPER in args:
                    op = args[args.index(runtime.HELPER) + 1]
                    if op.startswith("export-"):
                        buffer = io.BytesIO()
                        with tarfile.open(fileobj=buffer, mode="w") as archive:
                            member = tarfile.TarInfo("report.json")
                            data = b'{"fixture":true}'
                            member.size = len(data)
                            archive.addfile(member, io.BytesIO(data))
                        output = buffer.getvalue()
                    else:
                        output = json.dumps(
                            {"bytes": 99} if op == "task-usage" else {"ok": True}
                        )
            else:
                raise AssertionError("Unexpected Docker operation " + repr(args))
        except KeyError:
            code = 1
        return subprocess.CompletedProcess(
            command,
            code,
            output.encode() if isinstance(output, str) else output,
            b"fixture error" if code else b"",
        )


class RootlessManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        control = self.root / "control"
        (control / "scripts/local_ci").mkdir(parents=True)
        (control / "scripts/local_ci/trusted.py").write_text("# trusted fixture\n")
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ),
        ):
            subprocess.run(
                ["git", "-C", str(control), *args], check=True, capture_output=True
            )
        archive = self.root / "llvm.tar"
        with tarfile.open(archive, "w") as tar:
            for name in (
                "llvm/bin/clang",
                "llvm/include/llvm/Header.h",
                "llvm/lib/libLLVM.so",
            ):
                member = tarfile.TarInfo(name)
                member.size = 4
                tar.addfile(member, io.BytesIO(b"llvm"))
        self.sha = "a" * 40
        profile = {
            "name": "triton-31",
            "triton_version": "3.1",
            "image": "registry.invalid/base@sha256:" + "b" * 64,
            "llvm_hash": self.sha,
            "backend_enabled": False,
            "llvm": {
                "mode": "archive",
                "commit": self.sha,
                "archive": str(archive),
                "sha256": file_digest(archive),
            },
            "env": {"SEED_PYTHON": "/opt/venv/bin/python"},
            "validation_commands": {
                key: ["validate-" + key]
                for key in (
                    "environment",
                    "frontend_build",
                    "wheel_install_import",
                    "frontend_smoke",
                )
            },
        }
        self.config = {
            "control_root": str(control),
            "runtime": {
                "kind": "docker-rootless",
                "endpoint": "unix:///run/user/1000/docker.sock",
            },
            "resources": {"cpus": 2, "memory_bytes": 1048576, "pids_limit": 128},
            "profiles": {"release/3.1": profile},
        }
        self.fake = FakeDocker()
        self.manager = EnvironmentManager(
            self.config, self.root / "state", runner=self.fake
        )
        self.task = {
            "worker_revision_sha": subprocess.check_output(
                ["git", "-C", str(control), "rev-parse", "HEAD"]).decode().strip(),
            "task_id": "d" * 64,
            "target_branch": "release/3.1",
            "llvm_hash": self.sha,
            "tested_sha": "e" * 40,
            "base_sha": "f" * 40,
        }

    def commit_control_update(self):
        root = Path(self.config["control_root"])
        (root / "scripts/local_ci/trusted.py").write_text("# next trusted revision\n")
        for args in (("add", "."), ("-c", "user.name=Fixture", "-c",
                     "user.email=fixture@example.invalid", "commit", "-qm", "update")):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()

    def test_control_updates_revalidate_without_rebuilding_and_pin_old_attempt(self):
        first = self.acquire()
        old_source = Path(first["control_snapshot"]["source"])
        revision = self.commit_control_update()
        second = self.acquire({**self.task, "task_id": "2" * 64, "worker_revision_sha": revision})
        self.assertEqual(first["image_id"], second["image_id"])
        self.assertNotEqual(first["control_snapshot"], second["control_snapshot"])
        self.assertEqual(second["control_revision"], revision)
        self.assertEqual((old_source / "scripts/local_ci/trusted.py").read_text(), "# trusted fixture\n")
        self.assertEqual(sum(c[3] == "build" for c in self.fake.commands), 1)
        self.assertEqual(sum("local-ci.kind=image-validation" in c for c in self.fake.commands), 2)
        self.assertEqual(self.manager.recover_task(first)["status"], "same_attempt")

    def test_control_and_validation_do_not_enter_dependency_context(self):
        context = self.root / "context"
        context.mkdir()
        self.manager._build_context(self.manager._profile("release/3.1", self.sha), context)
        self.assertFalse((context / "control").exists())
        self.assertTrue((context / "image_prepare.py").is_file())
        recipe = json.loads((context / "image-recipe.json").read_text())
        self.assertNotIn("control_revision", recipe)
        self.assertNotIn("validation_commands", recipe)

    def test_config_routing_and_validation_changes_do_not_rebuild_dependencies(self):
        first = self.manager.ensure_image("release/3.1", self.sha)
        self.config["branch_profiles"] = {"CI_dev": "release/3.1"}
        self.config["resources"]["cpus"] = 4
        profile = self.config["profiles"]["release/3.1"]
        profile["daily_calendar"] = "*-*-* 06:00:00"
        profile["validation_commands"]["environment"] = ["new-environment-validation"]
        second = self.manager.ensure_image("release/3.1", self.sha)
        self.assertEqual(first["image_id"], second["image_id"])
        self.assertTrue(any("new-environment-validation" in c for c in self.fake.commands))
        profile["image"] = "sha256:" + "c" * 64
        third = self.manager.ensure_image("release/3.1", self.sha)
        self.assertNotEqual(second["image_id"], third["image_id"])

    def test_control_mounts_are_readonly_and_bound_to_registry(self):
        handle = self.acquire()
        creates = [c for c in self.fake.commands if c[3] == "create"]
        self.assertTrue(all(runtime.control_mount_arguments(handle["control_snapshot"])[1] in c for c in creates))
        with self.assertRaisesRegex(EnvironmentError, "trusted registry"):
            self.manager._verify({**handle, "control_snapshot": {**handle["control_snapshot"], "source": "/tmp/wrong"}})
        mount = next(m for m in self.fake.containers[handle["container_id"]]["Mounts"]
                     if m["Destination"] == runtime.CONTROL_TARGET)
        mount["RW"] = True
        with self.assertRaisesRegex(EnvironmentError, "Control snapshot mount"):
            self.manager.recover_task(handle)

    def test_changed_snapshot_is_rejected(self):
        handle = self.acquire()
        (Path(handle["control_snapshot"]["source"]) / "scripts/local_ci/trusted.py").write_text("changed")
        with self.assertRaisesRegex(EnvironmentError, "snapshot"):
            self.manager.recover_task(handle)
        with self.assertRaisesRegex(EnvironmentError, "snapshot"):
            self.manager._control_snapshot(handle["control_revision"])

    def test_task_control_revision_mismatch_fails_before_build(self):
        with self.assertRaisesRegex(EnvironmentError, "worker_revision_sha"):
            self.acquire({**self.task, "worker_revision_sha": "0" * 40})
        self.assertFalse(self.fake.images)

    def test_snapshot_excludes_untracked_files_and_rejects_escaping_links(self):
        root = Path(self.config["control_root"])
        (root / "secrets").mkdir()
        (root / "secrets/auth.json").write_text("private fixture")
        snapshot = self.manager._control_snapshot(self.task["worker_revision_sha"])
        self.assertFalse((Path(snapshot["source"]) / "secrets").exists())
        self.assertFalse((Path(snapshot["source"]) / ".git").exists())
        (root / "scripts/local_ci/escape").symlink_to("/etc/passwd")
        revision = self.commit_control_update()
        with self.assertRaisesRegex(EnvironmentError, "absolute target"):
            self.manager._control_snapshot(revision)

    def test_legacy_image_is_adopted_only_after_mounted_control_validation(self):
        first = self.manager.ensure_image("release/3.1", self.sha)
        state = self.manager._load()
        row = state["images"][first["release_id"]]
        row["recipe_digest"] = runtime.fingerprint([
            self.manager._profile("release/3.1", self.sha), self.manager.uids, self.manager.gids])
        row.pop("control_delivery")
        row.pop("validation_digest")
        self.manager._save(state)
        revision = self.commit_control_update()
        second = self.manager.ensure_image("release/3.1", self.sha)
        self.assertEqual(first["image_id"], second["image_id"])
        self.assertEqual(second["validation"]["control_revision"], revision)
        self.assertEqual(second["control_delivery"], "snapshot-mount-legacy-image")
        self.assertEqual(self.manager.ensure_image("release/3.1", self.sha)["image_id"], first["image_id"])
        self.assertEqual(sum(c[3] == "build" for c in self.fake.commands), 1)

    def test_failed_new_control_validation_does_not_rebuild_or_record_pass(self):
        first = self.manager.ensure_image("release/3.1", self.sha)
        self.commit_control_update()
        self.fake.fail_check = "validate-frontend_smoke"
        with self.assertRaises(EnvironmentError):
            self.manager.ensure_image("release/3.1", self.sha)
        row = self.manager._load()["images"][first["release_id"]]
        self.assertEqual(row["validation_digest"], first["validation_digest"])
        self.assertEqual(sum(c[3] == "build" for c in self.fake.commands), 1)

    def test_revalidation_cleanup_failure_blocks_new_work(self):
        first = self.manager.ensure_image("release/3.1", self.sha)
        self.commit_control_update()
        self.fake.fail_stop = True
        with self.assertRaises(EnvironmentError):
            self.manager.ensure_image("release/3.1", self.sha)
        row = self.manager._load()["images"][first["release_id"]]
        self.assertFalse(row["validation_cleanup_confirmed"])
        with self.assertRaisesRegex(EnvironmentError, "stop is unconfirmed"):
            self.manager.ensure_image("release/3.1", self.sha)

    def test_offline_foundation_tag_must_match_pinned_digest(self):
        profile = self.config["profiles"]["release/3.1"]
        profile["local_image_tag"] = "ci/foundation:local"
        self.fake.images[profile["image"]] = {"Id": "sha256:" + "b" * 64}
        self.fake.images[profile["local_image_tag"]] = {"Id": "sha256:" + "b" * 64}
        self.manager.ensure_image("release/3.1", self.sha)
        build = next(command for command in self.fake.commands if command[3] == "build")
        self.assertIn("BASE_IMAGE=ci/foundation:local", build)
        self.assertIn("--pull=false", build)
        self.fake.images[profile["local_image_tag"]] = {"Id": "sha256:" + "c" * 64}
        with self.assertRaisesRegex(EnvironmentError, "does not match"):
            self.manager._foundation_reference(profile)

    def acquire(self, task=None, run="run-1"):
        return self.manager.acquire_task(task or self.task, run)

    def test_branch_alias_reuses_profile_without_changing_task_identity(self):
        self.config["branch_profiles"] = {"CI_dev": "release/3.1"}
        image = self.manager.ensure_image("release/3.1", self.sha)
        task = {**self.task, "target_branch": "CI_dev", "event_kind": "pull_request"}
        before = json.dumps(task, sort_keys=True)
        handle = self.acquire(task)
        self.assertEqual(json.dumps(task, sort_keys=True), before)
        self.assertEqual(handle["task"], task)
        self.assertEqual(handle["target_branch"], "CI_dev")
        self.assertEqual(handle["profile_branch"], "release/3.1")
        self.assertEqual(handle["image_id"], image["image_id"])
        self.assertEqual(handle["image_release_id"], image["release_id"])
        self.assertEqual(list(self.manager.health()["active_images"]), ["release/3.1"])
        self.assertEqual(sum(c[3] == "build" for c in self.fake.commands), 1)
        self.assertEqual(self.acquire(task)["attempt_id"], handle["attempt_id"])
        self.manager.destroy_task(handle, keep_data=False)
        self.assertEqual(self.manager.generations()[handle["attempt_id"]]["state"], "removed")

    def test_branch_alias_preserves_exact_mounted_llvm_requirement(self):
        self.mounted_llvm()
        self.config["branch_profiles"] = {"CI_dev": "release/3.1"}
        with self.assertRaisesRegex(EnvironmentError, "New LLVM requires"):
            self.acquire({**self.task, "target_branch": "CI_dev", "llvm_hash": "b" * 40})
        self.assertFalse(self.fake.images)
        self.assertFalse(self.fake.containers)

    def test_unmapped_branch_has_no_implicit_fallback(self):
        with self.assertRaisesRegex(EnvironmentError, "Trusted profile"):
            self.acquire({**self.task, "target_branch": "CI_dev"})
        self.config["branch_profiles"] = {"CI_dev": "release/3.1"}
        with self.assertRaisesRegex(EnvironmentError, "Trusted profile"):
            self.acquire({**self.task, "target_branch": "main"})

    def test_invalid_branch_profile_mappings_fail_closed(self):
        for mappings in (None, [], {"CI_dev": "missing"}, {"CI_dev": []},
                         {"CI_dev": "alias", "alias": "release/3.1"},
                         {"release/3.1": "release/3.1"},
                         {"CI_dev_forPR": "release/3.1"}, {"": "release/3.1"},
                         {"CI_dev\n": "release/3.1"}, {"*": "release/3.1"}):
            with self.subTest(mappings=mappings):
                self.config["branch_profiles"] = mappings
                with self.assertRaisesRegex(EnvironmentError, "branch_profiles"):
                    self.acquire({**self.task, "target_branch": "CI_dev"})
        self.assertFalse(self.fake.commands)

    def mounted_llvm(self):
        root = self.root / "dependencies"
        source = root / ("llvm-" + self.sha)
        source.mkdir(parents=True)
        (source / "version.txt").write_text("trusted LLVM fixture")
        root.chmod(0o755)
        source.chmod(0o755)
        (source / "version.txt").chmod(0o644)
        self.config["dependency_root"] = str(root)
        profile = self.config["profiles"]["release/3.1"]
        profile["llvm"] = {"mode": "mount", "commit": self.sha}
        profile["mounts"] = [{
            "source": str(source),
            "target": "/opt/local-ci/runtime/deps/llvm-" + self.sha,
            "read_only": True,
            "sha256": tree_digest(source),
        }]
        return profile, source

    def test_mounted_toolchain_is_not_copied_to_image_context(self):
        profile, source = self.mounted_llvm()
        checked = self.manager._profile("release/3.1", self.sha)
        context = self.root / "context"
        context.mkdir()
        self.manager._build_context(checked, context)
        target = context / "payload/deps" / source.name
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.manager.directory / "downloads").exists())

    def test_image_validation_and_tasks_share_verified_readonly_dependencies(self):
        profile, source = self.mounted_llvm()
        handle = self.acquire()
        self.assertEqual(handle["dependency_mounts"], profile["mounts"])
        creates = [c for c in self.fake.commands if c[3] == "create"]
        self.assertEqual(len(creates), 2)
        expected = "type=bind,source=" + str(source) + ",target=" + profile["mounts"][0]["target"] + ",readonly,bind-recursive=disabled"
        self.assertTrue(all(expected in command for command in creates))
        mount = self.fake.containers[handle["container_id"]]["Mounts"][-1]
        mount["RW"] = True
        with self.assertRaisesRegex(EnvironmentError, "dependency mount"):
            self.manager.recover_task(handle)

    def test_dependency_drift_blocks_new_tasks_and_resume(self):
        profile, source = self.mounted_llvm()
        handle = self.acquire()
        (source / "version.txt").write_text("replaced toolchain")
        with self.assertRaisesRegex(EnvironmentError, "SHA256 changed"):
            self.manager.recover_task(handle)
        with self.assertRaisesRegex(EnvironmentError, "SHA256 changed"):
            self.acquire({**self.task, "task_id": "2" * 64})

    def test_missing_dependencies_do_not_block_management_evidence_recovery(self):
        profile, source = self.mounted_llvm()
        handle = self.acquire()
        source.rename(source.with_name("moved"))
        del self.fake.containers[handle["container_id"]]
        self.assertEqual(self.manager.task_usage(handle), 99)
        recovery = self.fake.containers[self.manager.generations()[handle["attempt_id"]]["recovery_container_id"]]
        self.assertEqual({m["Destination"] for m in recovery["Mounts"]},
                         {"/task", "/codex", runtime.CONTROL_TARGET})

    def test_dependency_configuration_rejects_unsafe_or_mutable_mounts(self):
        profile, source = self.mounted_llvm()
        entry = profile["mounts"][0]
        for key, value in (("read_only", False), ("target", "/task"), ("target", "/opt/venv"),
                           ("source", str(self.root)), ("sha256", "bad")):
            original = entry[key]
            entry[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(EnvironmentError):
                dependency_mounts(self.config, profile, verify_content=True)
            entry[key] = original
        outside = source / "external"
        outside.symlink_to(self.root / "control")
        with self.assertRaisesRegex(EnvironmentError, "link escapes"):
            dependency_mounts(self.config, profile, verify_content=True)
        outside.unlink()
        (source / "version.txt").chmod(0o666)
        with self.assertRaisesRegex(EnvironmentError, "no group/other writes"):
            dependency_mounts(self.config, profile, verify_content=True)

    def test_mounted_llvm_commit_must_match_requested_version(self):
        profile, _ = self.mounted_llvm()
        profile["llvm"]["commit"] = "b" * 40
        with self.assertRaisesRegex(EnvironmentError, "LLVM"):
            self.acquire()

    def test_fixed_endpoint_and_four_distinct_nonroot_users(self):
        self.assertEqual(
            docker_command(self.config, "info"),
            ["docker", "--host", self.config["runtime"]["endpoint"], "info"],
        )
        uids, gids = identities(self.config)
        self.assertEqual(len(set(uids.values())), 4)
        self.assertNotIn(0, uids.values())
        self.assertEqual(gids["candidate"], gids["diagnostic"])
        self.assertNotEqual(gids["codex"], gids["diagnostic"])
        for role in ("candidate", "codex"):
            with self.subTest(role=role), self.assertRaises(EnvironmentError):
                identities({"identities": {role: 0}})

    def test_rootful_daemon_and_missing_limits_fail_closed(self):
        self.fake.rootless = False
        with self.assertRaisesRegex(EnvironmentError, "rootless"):
            self.acquire()
        self.fake.rootless = True
        for resources in (
            {},
            {"cpus": float("nan"), "memory_bytes": 1, "pids_limit": 1},
        ):
            self.config["resources"] = resources
            with self.assertRaises(EnvironmentError):
                self.manager._limits()

    def test_two_tasks_share_only_the_validated_image(self):
        first = self.acquire()
        second = self.acquire({**self.task, "task_id": "1" * 64})
        self.assertEqual(first["image_id"], second["image_id"])
        self.assertNotEqual(first["container_id"], second["container_id"])
        self.assertTrue(
            set(first["volumes"].values()).isdisjoint(second["volumes"].values())
        )
        self.assertNotEqual(first["rpc_host_dir"], second["rpc_host_dir"])
        mounts = self.fake.containers[first["container_id"]]["Mounts"]
        self.assertFalse(
            any(item["Source"] == first["workspace_host"] for item in mounts)
        )
        command = next(
            command
            for command in self.fake.commands
            if "create" in command and first["container"] in command
        )
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges=true", command)
        self.assertEqual(command[command.index("--user") + 1], "0:0")
        self.assertNotIn("--privileged", command)

    def test_same_attempt_restart_and_mount_identity(self):
        handle = self.acquire()
        self.manager.stop_task(handle)
        recovered = self.manager.recover_task(handle)
        self.assertEqual(recovered["status"], "same_attempt")
        self.assertEqual(recovered["handle"]["attempt_id"], handle["attempt_id"])
        self.fake.containers[handle["container_id"]]["Mounts"][0]["Name"] = (
            "other-pr-volume"
        )
        with self.assertRaisesRegex(EnvironmentError, "mount"):
            self.manager.recover_task(handle)

    def test_missing_container_requires_new_attempt(self):
        first = self.acquire()
        del self.fake.containers[first["container_id"]]
        self.assertEqual(self.manager.recover_task(first)["status"], "rebuild_required")
        second = self.acquire()
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertNotEqual(first["volumes"], second["volumes"])

    def test_daemon_replacement_is_not_reported_as_lost_container(self):
        handle = self.acquire()
        self.fake.daemon = "different-daemon"
        with self.assertRaisesRegex(EnvironmentError, "daemon identity"):
            self.manager.recover_task(handle)

    def test_lost_container_has_management_only_recovery_for_export_and_usage(self):
        handle = self.acquire()
        del self.fake.containers[handle["container_id"]]
        self.assertEqual(self.manager.task_usage(handle), 99)
        self.assertTrue(
            self.manager.export_execution(handle, "b" * 32, self.root / "saved")[
                "exported"
            ]
        )
        row = self.manager.generation(handle["attempt_id"])
        self.assertTrue(row["recovery_only"])
        recovered = self.fake.containers[row["recovery_container_id"]]
        self.assertFalse(recovered["State"]["Running"])
        self.assertEqual(
            {m["Destination"] for m in recovered["Mounts"]},
            {"/task", "/codex", runtime.CONTROL_TARGET}
        )
        self.assertFalse(recovered["Mounts"][0]["RW"])
        self.assertEqual(
            self.manager.recover_task(handle)["status"], "rebuild_required"
        )
        with self.assertRaises(EnvironmentError):
            self.manager.prepare_execution(handle, "b" * 32, "candidate")
        self.manager.destroy_task(handle, keep_data=False)
        self.assertNotIn(row["recovery_container_id"], self.fake.containers)

    def test_missing_codex_volume_preserves_task_evidence(self):
        handle = self.acquire()
        del self.fake.containers[handle["container_id"]]
        del self.fake.volumes[handle["volumes"]["codex"]]
        self.assertTrue(
            self.manager.export_execution(handle, "b" * 32, self.root / "saved")[
                "exported"
            ]
        )
        self.assertEqual(self.manager.task_usage(handle), 99)
        self.assertTrue(
            self.manager.generation(handle["attempt_id"])["credential_volume_missing"]
        )
        self.assertIn(handle["volumes"]["task"], self.fake.volumes)

    def test_missing_task_or_both_volumes_records_explicit_evidence_loss(self):
        for both in (False, True):
            handle = self.acquire({**self.task, "task_id": ("3" if both else "4") * 64})
            del self.fake.containers[handle["container_id"]]
            del self.fake.volumes[handle["volumes"]["task"]]
            if both:
                del self.fake.volumes[handle["volumes"]["codex"]]
            result = self.manager.export_execution(
                handle, "b" * 32, self.root / "saved"
            )
            self.assertEqual(
                result, {"exported": False, "evidence_loss": "task_volume_missing"}
            )
            self.assertEqual(self.manager.task_usage(handle), 0)
            self.assertEqual(
                self.manager.generation(handle["attempt_id"])["evidence_loss"],
                "task_volume_missing",
            )
            self.manager.destroy_task(handle, keep_data=False)
            self.assertNotIn(handle["volumes"]["codex"], self.fake.volumes)

    def test_failed_image_validation_keeps_previous_release(self):
        handle = self.acquire()
        self.fake.fail_check = "validate-frontend_smoke"
        with self.assertRaises(EnvironmentError):
            self.manager.rotate(self.task["target_branch"])
        self.assertEqual(
            self.manager.health()["active_images"][self.task["target_branch"]],
            handle["image_release_id"],
        )
        self.assertFalse(
            any(
                c["Config"]["Labels"].get("local-ci.kind") == "image-validation"
                for c in self.fake.containers.values()
            )
        )

    def test_backend_release_has_real_torch_tpu_ppl_and_rebuild_probes(self):
        profile = self.config["profiles"]["release/3.1"]
        profile.update(triton_version="3.0", backend_enabled=True)
        profile["env"].update(PPL_ROOT="/opt/ppl", BACKEND_PATH="/opt/backend")
        profile["validation_commands"].update(
            {
                key: ["validate-" + key]
                for key in ("backend_rebuild", "backend_smoke_jit")
            }
        )
        handle = self.acquire()
        commands = self.fake.commands
        self.assertTrue(handle["backend_enabled"])
        self.assertTrue(any("import torch,torch_tpu" in args for args in commands))
        self.assertTrue(
            any(args[-3:] == ["test", "-d", "/opt/ppl"] for args in commands)
        )
        profile["triton_version"] = "3.1"
        with self.assertRaisesRegex(EnvironmentError, "3.0"):
            self.manager.rotate("release/3.1")

    def test_model_secrets_only_use_stdin_and_incremental_session(self):
        handle = self.acquire()
        layout = self.manager.deploy_session(handle, {}, {})
        self.assertEqual(layout["rpc_socket"], "/run/local-ci-rpc/broker.sock")
        self.manager.deploy_session(
            handle, {"auth.json": "secret-auth"}, {"API_KEY": "secret-value"}
        )
        self.assertNotIn("secret-value", repr(self.fake.commands))
        self.assertNotIn("secret-auth", repr(self.fake.commands))
        self.assertIn(b"secret-value", self.fake.inputs[-1])

    def test_native_workspace_is_bound_to_task_sha_and_separate_private_export(self):
        handle = self.acquire()
        handle["task"] = {**handle["task"], "tested_sha": "0" * 40}
        self.manager.prepare_native_workspace(handle)
        command = next(
            command
            for command in reversed(self.fake.commands)
            if "prepare-native-workspace" in command
        )
        self.assertEqual(
            json.loads(command[-1]), {"expected_sha": self.task["tested_sha"]}
        )
        self.assertEqual(command[command.index("--user") + 1], "0:0")
        destination = self.root / "private-native"
        result = self.manager.export_native_evidence(handle, destination)
        self.assertTrue(result["exported"])
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertTrue(
            any("export-native-evidence" in command for command in self.fake.commands)
        )
        self.assertFalse(
            any("export-evidence" in command for command in self.fake.commands)
        )

    def test_native_export_recovers_stopped_container_and_reports_lost_codex_volume(
        self,
    ):
        handle = self.acquire()
        self.manager.stop_task(handle)
        self.assertTrue(
            self.manager.export_native_evidence(handle, self.root / "native-a")[
                "exported"
            ]
        )
        self.assertFalse(
            self.fake.containers[handle["container_id"]]["State"]["Running"]
        )
        del self.fake.containers[handle["container_id"]]
        del self.fake.volumes[handle["volumes"]["codex"]]
        self.assertEqual(
            self.manager.export_native_evidence(handle, self.root / "native-b"),
            {
                "exported": False,
                "evidence_loss": "codex_volume_missing",
            },
        )
        self.assertTrue(
            self.manager.export_evidence(handle, self.root / "formal-evidence")[
                "exported"
            ]
        )
        row = self.manager.generation(handle["attempt_id"])
        self.assertFalse(
            self.fake.containers[row["recovery_container_id"]]["State"]["Running"]
        )

    def test_offline_foundation_import_is_checksum_bound_and_not_validated_release(
        self,
    ):
        archive = self.config["profiles"]["release/3.1"]["llvm"]["archive"]
        identity = "sha256:" + "c" * 64
        self.fake.images[identity] = {"Id": identity}
        record = self.manager.import_foundation(
            archive, file_digest(Path(archive)), identity
        )
        self.assertFalse(record["validated"])
        self.assertEqual(self.manager.health()["images"], [])

    def test_unconfirmed_image_validation_stop_blocks_until_owned_cleanup(self):
        self.fake.fail_stop = True
        with self.assertRaises(EnvironmentError):
            self.manager.rotate("release/3.1")
        self.assertFalse(
            self.manager.health()["images"][0]["validation_cleanup_confirmed"]
        )
        with self.assertRaisesRegex(EnvironmentError, "unconfirmed"):
            self.acquire()
        self.fake.fail_stop = False
        self.manager.collect_retired()
        self.acquire()

    def test_stopped_evidence_export_is_inert_and_stops_again(self):
        handle = self.acquire()
        self.manager.stop_task(handle)
        self.manager.export_execution(handle, "a" * 32, self.root / "evidence")
        self.assertEqual(
            json.loads((self.root / "evidence/report.json").read_text()),
            {"fixture": True},
        )
        self.assertFalse(
            self.fake.containers[handle["container_id"]]["State"]["Running"]
        )
        self.assertEqual(self.manager.task_usage(handle), 99)
        self.assertFalse(
            self.fake.containers[handle["container_id"]]["State"]["Running"]
        )

    def test_destroy_retains_then_removes_exact_owned_volumes_idempotently(self):
        handle = self.acquire()
        self.assertTrue(self.manager.destroy_task(handle, keep_data=True)["retained"])
        self.manager.release(handle["task_id"])
        self.assertEqual(len(self.fake.volumes), 2)
        self.assertTrue(self.manager.destroy_task(handle, keep_data=False)["removed"])
        self.assertTrue(self.manager.destroy_task(handle, keep_data=False)["removed"])
        self.assertEqual(self.fake.volumes, {})

    def test_partial_delete_and_missing_container_cleanup_are_resumable(self):
        handle = self.acquire()
        self.fake.fail_volume_rm = True
        with self.assertRaises(EnvironmentError):
            self.manager.destroy_task(handle, keep_data=False)
        self.fake.fail_volume_rm = False
        self.assertTrue(self.manager.destroy_task(handle, keep_data=False)["removed"])
        self.assertEqual(self.fake.volumes, {})

    def test_failed_stop_blocks_new_work_until_confirmed(self):
        handle = self.acquire()
        self.fake.fail_stop = True
        with self.assertRaises(EnvironmentError):
            self.manager.stop_task(handle)
        with self.assertRaisesRegex(EnvironmentError, "unconfirmed"):
            self.acquire({**self.task, "task_id": "2" * 64})
        self.fake.fail_stop = False
        self.manager.stop_task(handle)
        self.manager.release(handle["task_id"])
        self.acquire({**self.task, "task_id": "2" * 64})

    def test_image_retention_protects_live_attempt(self):
        handle = self.acquire()
        self.manager.rotate("release/3.1")
        self.config["generation_retention_hours"] = 0
        self.assertIn(
            handle["image_release_id"], self.manager.collect_retired()["protected"]
        )
        self.manager.destroy_task(handle, keep_data=False)
        self.assertIn(
            handle["image_release_id"], self.manager.collect_retired()["protected"]
        )
        self.manager.rotate("release/3.1")
        self.assertIn(
            handle["image_release_id"], self.manager.collect_retired()["removed"]
        )

    def test_active_release_is_preferred_and_rollback_is_validated(self):
        first = self.manager.rotate("release/3.1")
        second = self.manager.rotate("release/3.1")
        self.assertEqual(self.acquire()["image_release_id"], second["release_id"])
        self.manager.rollback_image("release/3.1", first["release_id"])
        self.assertEqual(
            self.acquire({**self.task, "task_id": "5" * 64})["image_release_id"],
            first["release_id"],
        )
        self.config["profiles"]["release/3.1"]["env"]["EXTRA_SETTING"] = "changed"
        with self.assertRaisesRegex(EnvironmentError, "Rollback"):
            self.manager.rollback_image("release/3.1", second["release_id"])

    def test_partial_creation_without_container_id_can_export_usage_and_cleanup(self):
        self.manager.ensure_image("release/3.1", self.sha)
        self.fake.fail_create = True
        with self.assertRaises(EnvironmentError):
            self.acquire()
        self.fake.fail_create = False
        handle = next(iter(self.manager.generations().values()))
        self.assertIsNone(handle["container_id"])
        self.assertEqual(
            self.manager.recover_task(handle)["status"], "rebuild_required"
        )
        self.assertEqual(self.manager.task_usage(handle), 99)
        self.manager.destroy_task(handle, keep_data=False)
        self.assertEqual(self.fake.volumes, {})

    def test_failed_build_records_durable_log_path_without_session_payload(self):
        self.fake.fail_check = "validate-frontend_build"
        with self.assertRaisesRegex(EnvironmentError, "image log"):
            self.manager.rotate("release/3.1")
        image = self.manager.health()["images"][0]
        path = Path(image["log_path"])
        self.assertTrue(path.exists())
        self.assertIn("exit=1", path.read_text())

    def test_checksum_cache_revalidates_corruption(self):
        profile = self.config["profiles"]["release/3.1"]["llvm"]
        cache = self.manager._download(profile["archive"], profile["sha256"])
        cache.write_bytes(b"changed")
        self.assertEqual(
            file_digest(self.manager._download(profile["archive"], profile["sha256"])),
            profile["sha256"],
        )
        with self.assertRaisesRegex(EnvironmentError, "SHA256"):
            self.manager._download(profile["archive"], "0" * 64)

    def test_unsafe_archive_cannot_escape_destination(self):
        path = self.root / "bad.tar"
        with tarfile.open(path, "w") as archive:
            item = tarfile.TarInfo("../escape")
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
        with self.assertRaises(EnvironmentError):
            extract_verified_archive(path, self.root / "unpacked", file_digest(path), 0)
        self.assertFalse((self.root / "escape").exists())

    def test_real_subprocess_cancellation_reaps_process_group(self):
        self.manager.runner = subprocess.run
        self.manager.cancel_event = threading.Event()
        timer = threading.Timer(0.2, self.manager.cancel_event.set)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        with self.assertRaisesRegex(EnvironmentError, "cancelled"):
            self.manager._run(
                [sys.executable, "-c", "import time;time.sleep(30)"], timeout=10
            )
        self.assertLess(time.monotonic() - start, 3)

    def test_export_rejects_changes_to_already_saved_evidence(self):
        handle = self.acquire()
        dest = self.root / "out"
        dest.mkdir()
        (dest / "report.json").write_text("immutable original")
        with self.assertRaisesRegex(EnvironmentError, "change saved evidence"):
            self.manager.export_execution(handle, "a" * 32, dest)

    def test_no_adoption_of_legacy_rootful_registry(self):
        self.manager.registry.write_text(
            json.dumps({"schema": "triton-anchor-local-ci-environments/v1"})
        )
        with self.assertRaisesRegex(EnvironmentError, "legacy"):
            self.manager.health()

    def test_plain_image_tag_and_uncommitted_controls_are_rejected(self):
        self.config["profiles"]["release/3.1"]["image"] = "base:latest"
        with self.assertRaisesRegex(EnvironmentError, "immutable"):
            self.acquire()
        self.config["profiles"]["release/3.1"]["image"] = "sha256:" + "b" * 64
        (Path(self.config["control_root"]) / "scripts/local_ci/trusted.py").write_text(
            "changed"
        )
        with self.assertRaisesRegex(EnvironmentError, "committed"):
            self.acquire()


if __name__ == "__main__":
    unittest.main()
