"""Control provenance regression checks use temporary local Git repositories only."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control.runtime import control


class ControlIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-control-review-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        # Temp roots may be under the caller's repository; Git must not inherit it.
        self.git_boundary = patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.root.parent)})
        self.git_boundary.start()
        self.addCleanup(self.git_boundary.stop)
        for scope in control.SCOPES:
            directory = self.root / scope
            directory.mkdir(parents=True)
            (directory / "entry.txt").write_bytes(b"trusted control\n")
        self.location = patch.object(control, "_implementation_root", return_value=self.root)
        self.location.start()
        self.addCleanup(self.location.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, encoding="utf-8").strip()

    def commit(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Local test")
        self.git("config", "user.email", "local-test@localhost")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("commit", "-qm", "trusted test control")
        return self.git("rev-parse", "HEAD")

    def test_clean_exact_control_is_verified(self):
        sha = self.commit()
        result = control.verify_control({}, {"worker_revision_sha": sha})
        self.assertTrue(result["verified"])
        self.assertEqual(result["actual_sha"], sha)
        self.assertEqual(len(result["files"]), 3)

    def test_another_worker_revision_cannot_be_claimed(self):
        self.commit()
        with self.assertRaises(ValueError):
            control.verify_control({}, {"worker_revision_sha": "a" * 40})

    def test_dirty_or_ignored_extra_code_is_rejected(self):
        sha = self.commit()
        (self.root / "scripts/local_ci/entry.txt").write_bytes(b"modified control\n")
        with self.assertRaises(ValueError):
            control.verify_control({}, {"worker_revision_sha": sha})
        self.git("checkout", "--", "scripts/local_ci/entry.txt")
        (self.root / ".git/info/exclude").write_text("extra.py\n", encoding="utf-8")
        (self.root / "scripts/local_ci/extra.py").write_text("print('untracked')\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            control.verify_control({}, {"worker_revision_sha": sha})

    def test_assume_unchanged_cannot_hide_control_changes(self):
        sha = self.commit()
        self.git("update-index", "--assume-unchanged", "scripts/local_ci/entry.txt")
        (self.root / "scripts/local_ci/entry.txt").write_bytes(b"modified but hidden from git diff\n")
        self.assertEqual(self.git("diff", "--name-only", "HEAD"), "")
        with self.assertRaises(ValueError):
            control.verify_control({}, {"worker_revision_sha": sha})

    def test_development_acceptance_never_claims_verified(self):
        self.commit()
        result = control.verify_control({"local_acceptance": True}, {"worker_revision_sha": "a" * 40})
        self.assertFalse(result["verified"])
        self.assertEqual(result["mode"], "local_acceptance")

    def test_config_cannot_select_an_unrelated_clean_tree(self):
        sha = self.commit()
        with self.assertRaises(ValueError):
            control.verify_control({"control_root": str(self.root / "different")}, {"worker_revision_sha": sha})

    def test_deployment_manifest_must_cover_every_actual_control_file(self):
        sha = "a" * 40
        files = {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob("*") if p.is_file()}
        manifest = self.root / "control-manifest.json"
        manifest.write_text(json.dumps({"schema": control.MANIFEST_SCHEMA, "revision": sha, "files": files}), encoding="utf-8")
        config = {"control_manifest": str(manifest)}
        self.assertTrue(control.verify_control(config, {"worker_revision_sha": sha})["verified"])
        (self.root / ".github/extra.yml").write_text("untrusted: change\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            control.verify_control(config, {"worker_revision_sha": sha})


if __name__ == "__main__":
    unittest.main()
