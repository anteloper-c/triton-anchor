"""Container helper operations tested against real temporary files and Unix UIDs."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from environments import container_fs as fs


@unittest.skipUnless(
    os.geteuid() == 0, "Namespace-root helper tests need isolated root test process"
)
class ContainerFsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.task, self.codex = self.root / "task", self.root / "codex"
        for field, value in (
            ("TASK", self.task),
            ("CODEX", self.codex),
            ("CONTROL", self.task / ".control"),
        ):
            patcher = mock.patch.object(fs, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = {
            "uids": {
                "candidate": 11001,
                "base": 11002,
                "diagnostic": 11003,
                "codex": 11004,
            },
            "gids": {
                "candidate": 11000,
                "base": 11000,
                "diagnostic": 11000,
                "codex": 11004,
            },
            "task_id": "d" * 64,
            "run_id": "run",
            "attempt_id": "a" * 32,
            "env": {"SEED_PYTHON": sys.executable},
            "backend_enabled": False,
        }
        fs.init_task(self.data)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "tracked.py").write_text("VALUE = 1\n")
        (self.source / "script.sh").write_text("#!/bin/sh\n")
        (self.source / "script.sh").chmod(0o755)
        (self.source / "link").symlink_to("tracked.py")
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
                ["git", "-C", str(self.source), *args], check=True, capture_output=True
            )
        self.sha = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.add(self.source, arcname=".")
        self.archive = buffer.getvalue()

    def import_variant(self, variant="candidate"):
        return fs.import_checkout(
            {
                "variant": variant,
                "expected_sha": self.sha,
                "sha256": hashlib.sha256(self.archive).hexdigest(),
            },
            io.BytesIO(self.archive),
        )

    def as_uid(self, role, script, *args):
        def identity():
            os.setgroups([])
            os.setgid(self.data["gids"][role])
            os.setuid(self.data["uids"][role])

        return subprocess.run(
            [sys.executable, "-I", "-c", script, *map(str, args)],
            preexec_fn=identity,
            capture_output=True,
        )

    def test_import_tracks_original_content_and_rejects_changed_identity(self):
        self.assertTrue(self.import_variant()["imported"])
        self.assertTrue(self.import_variant()["reused"])
        params = {"variant": "candidate", "expected_sha": self.sha}
        self.assertTrue(fs.verify_checkout(params)["verified"])
        path = self.task / "candidate/checkout/tracked.py"
        path.write_text("VALUE = 2\n")
        # An index flag must not hide mutation from the root-owned manifest.
        subprocess.run(
            [
                "git",
                "-C",
                str(path.parent),
                "-c",
                "safe.directory=" + str(path.parent),
                "update-index",
                "--assume-unchanged",
                "tracked.py",
            ],
            check=True,
            capture_output=True,
        )
        self.assertFalse(fs.verify_checkout(params)["verified"])

    def test_verification_never_runs_modified_git_hooks_or_configuration(self):
        self.import_variant()
        checkout = self.task / "candidate/checkout"
        (checkout / ".git/config").write_text("not git configuration\n")
        with mock.patch.object(
            fs,
            "git",
            side_effect=AssertionError("Git should not execute after PR starts"),
        ):
            self.assertTrue(
                fs.verify_checkout({"variant": "candidate", "expected_sha": self.sha})[
                    "verified"
                ]
            )

    def test_tracked_mode_symlink_and_parent_type_are_verified(self):
        self.import_variant()
        params = {"variant": "candidate", "expected_sha": self.sha}
        script = self.task / "candidate/checkout/script.sh"
        script.chmod(0o644)
        self.assertFalse(fs.verify_checkout(params)["verified"])
        script.chmod(0o755)
        link = script.parent / "link"
        link.unlink()
        link.symlink_to("script.sh")
        self.assertFalse(fs.verify_checkout(params)["verified"])

    def test_namespace_ownership_and_diagnostic_read_only_grant(self):
        self.import_variant()
        self.import_variant("base")
        fs.authorize_diagnostics()
        candidate = self.task / "candidate/checkout/tracked.py"
        base = self.task / "base/checkout/tracked.py"
        self.assertEqual(candidate.stat().st_uid, self.data["uids"]["candidate"])
        read = (
            "from pathlib import Path;import sys;assert Path(sys.argv[1]).read_text()"
        )
        write = "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('modified')"
        self.assertEqual(self.as_uid("diagnostic", read, candidate).returncode, 0)
        self.assertNotEqual(self.as_uid("diagnostic", write, candidate).returncode, 0)
        self.assertNotEqual(self.as_uid("candidate", write, base).returncode, 0)
        self.assertEqual(self.as_uid("candidate", write, candidate).returncode, 0)

    def test_codex_credentials_are_private_and_purge_preserves_session_history(self):
        fs.deploy_session(
            {
                "files": {"config.toml": "provider='company'", "auth.json": "private"},
                "environment": {"API_KEY": "private-key"},
            }
        )
        session = self.codex / "home/session-history.json"
        session.write_text("history")
        reader = (
            "from pathlib import Path;import sys;print(Path(sys.argv[1]).read_text())"
        )
        for role in ("candidate", "base", "diagnostic"):
            self.assertNotEqual(
                self.as_uid(role, reader, self.codex / "home/auth.json").returncode, 0
            )
            self.assertNotEqual(
                self.as_uid(role, reader, self.codex / "environment.json").returncode, 0
            )
        self.assertEqual(
            self.as_uid("codex", reader, self.codex / "home/auth.json").returncode, 0
        )
        fs.deploy_session(
            {
                "files": {"config.toml": "provider='company2'"},
                "environment": {"API_KEY": "private-key"},
            }
        )
        self.assertEqual((self.codex / "home/auth.json").read_text(), "private")
        fs.purge_credentials()
        self.assertFalse((self.codex / "home/auth.json").exists())
        self.assertFalse((self.codex / "environment.json").exists())
        self.assertTrue(session.exists())

    def test_execution_layout_diagnostics_and_read_only_scripts(self):
        ident = "1" * 32
        layout = fs.prepare_execution(
            {"execution_id": ident, "variant": "candidate", "diagnostic": True}
        )
        self.assertEqual(
            Path(layout["home"]), self.task / "diagnostics" / ident / "home"
        )
        self.assertEqual(Path(layout["artifact_dir"]).stat().st_uid, 11003)
        script = Path(
            fs.write_execution_file(
                {"execution_id": ident, "name": "program.py"}, b"print('diagnostic')"
            )["path"]
        )
        self.assertEqual(script.stat().st_uid, 0)
        self.assertFalse(script.stat().st_mode & 0o222)
        self.assertEqual(
            self.as_uid(
                "diagnostic",
                "from pathlib import Path;import sys;assert Path(sys.argv[1]).read_text()",
                script,
            ).returncode,
            0,
        )

    def test_fixed_paths_reject_symlink_escape_and_export_ignores_host_records(self):
        root = self.task / "artifacts" / ("2" * 32)
        root.mkdir()
        (root / "report.json").write_text("{}")
        (root / "escape").symlink_to("/etc/passwd")
        (root / "execution.log").write_text("fake log")
        (root / "executor-record.json").write_text("fake record")
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "symlinks"):
            fs.export_archive(root, output)
        (root / "escape").unlink()
        os.link(root / "report.json", root / "hardlink")
        with self.assertRaisesRegex(ValueError, "hardlinks"):
            fs.export_archive(root, io.BytesIO())
        (root / "hardlink").unlink()
        output = io.BytesIO()
        fs.export_archive(
            root, output, exclude=("execution.log", "executor-record.json")
        )
        with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
            self.assertEqual(archive.getnames(), ["report.json"])
        with self.assertRaises(ValueError):
            fs.read_file({"scope": "artifacts", "path": "2" * 32 + "/escape"})
        with self.assertRaises(ValueError):
            fs.checked(self.task, "../codex/home/auth.json")

    def test_runtime_info_reports_seed_then_existing_variant(self):
        result = fs.runtime_info()["candidate"]
        self.assertEqual(result["runtime_origin"], "seed")
        self.assertEqual(result["python_bin"], sys.executable)
        (self.task / "candidate/venv/bin").mkdir(parents=True)
        (self.task / "candidate/venv/bin/python").symlink_to(sys.executable)
        result = fs.runtime_info()["candidate"]
        self.assertTrue(result["python_available"])
        self.assertEqual(result["runtime_origin"], "variant")

    def test_usage_includes_private_session_volume_without_returning_contents(self):
        before = fs.task_usage()["bytes"]
        (self.codex / "home/session.json").write_bytes(b"private-session-data")
        (self.task / "candidate/log").write_bytes(b"log")
        self.assertEqual(
            fs.task_usage(), {"bytes": before + len(b"private-session-data") + 3}
        )

    def test_import_checksum_and_tar_traversal_fail_without_writing_outside(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            fs.extract(io.BytesIO(self.archive), self.root / "destination", "0" * 64)
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            member = tarfile.TarInfo("../escaped")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        payload = output.getvalue()
        with self.assertRaises(ValueError):
            fs.extract(
                io.BytesIO(payload),
                self.root / "destination",
                hashlib.sha256(payload).hexdigest(),
            )
        self.assertFalse((self.root / "escaped").exists())

    def test_experiment_identity_is_separate_and_resumable(self):
        self.import_variant()

        def make_venv(args, **kwargs):
            if "-c" in args:
                return subprocess.CompletedProcess(
                    args, 0, b'{"version":"3.10","paths":[]}'
                )
            root = Path(args[-1])
            (root / "bin").mkdir(parents=True)
            (root / "bin/python").write_text("fixture Python")
            return subprocess.CompletedProcess(args, 0)

        with (
            mock.patch.object(fs.subprocess, "run", side_effect=make_venv),
            mock.patch.object(Path, "glob", return_value=[]),
        ):
            result = fs.create_experiment(
                {"experiment_id": "repro-1", "variant": "candidate"}
            )
            second = fs.create_experiment(
                {"experiment_id": "repro-1", "variant": "candidate"}
            )
            self.assertEqual(second, result)
            self.assertTrue((Path(result["venv"]) / "bin/python").is_file())
            self.assertEqual(Path(result["root"]).stat().st_uid, 11003)
            with self.assertRaisesRegex(ValueError, "identity"):
                fs.create_experiment({"experiment_id": "repro-1", "variant": "base"})


if __name__ == "__main__":
    unittest.main()
