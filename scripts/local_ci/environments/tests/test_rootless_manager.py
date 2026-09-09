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
            "task_id": "d" * 64,
            "target_branch": "release/3.1",
            "llvm_hash": self.sha,
            "tested_sha": "e" * 40,
            "base_sha": "f" * 40,
        }

    def acquire(self, task=None, run="run-1"):
        return self.manager.acquire_task(task or self.task, run)

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
            {m["Destination"] for m in recovered["Mounts"]}, {"/task", "/codex"}
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
