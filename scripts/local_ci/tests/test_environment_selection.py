"""LLVM selection against actual frozen Git blobs and gitlinks; no network or Docker."""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control.runtime.common import git  # noqa: E402
from control.runtime.environment import EnvironmentSelectionError, resolve_profile  # noqa: E402


class EnvironmentSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.repository("source")
        self.recipe = self.root / "trusted-recipe"
        self.recipe.mkdir()
        (self.recipe / "Dockerfile").write_text("FROM trusted-base\nCOPY prepare_llvm.sh /recipe/\n")
        (self.recipe / "prepare_llvm.sh").write_text("# Trusted recipe fixture, never executed.\n")
        self.profile = {"id": "triton-3.0", "triton_version": "3.0", "llvm_revision": "a" * 40,
                        "container": {"name": "fixed-worker", "image": "trusted", "healthcheck": ["true"]},
                        "maintenance": {"recipe": {"context": str(self.recipe)}},
                        "tools": {"llvm_dir": "/opt/llvm"}}
        self.config = {"workspace_host": str(self.root / "workspace")}

    def repository(self, name):
        path = self.root / name
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        git(path, "config", "user.name", "LLVM environment fixture")
        git(path, "config", "user.email", "fixture@invalid")
        return path

    def commit(self, repository, files):
        for name, text in files.items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(repository, "add", ".")
        git(repository, "commit", "-m", "frozen source fixture")
        return git(repository, "rev-parse", "HEAD")

    def select(self, tested):
        return resolve_profile(self.config, SimpleNamespace(mirror=self.source), {"tested_sha": tested}, self.profile)

    def test_frozen_blob_wins_over_later_head_and_profile_remains_unchanged(self):
        tested = self.commit(self.source, {"triton/cmake/llvm-hash.txt": "b" * 40 + "\n"})
        self.commit(self.source, {"triton/cmake/llvm-hash.txt": "c" * 40 + "\n"})
        before = copy.deepcopy(self.profile)
        selected = self.select(tested)
        self.assertEqual(selected["llvm_revision"], "b" * 40)
        self.assertEqual(selected["container"]["name"], "fixed-worker")
        self.assertTrue(selected["llvm_selection"]["rebuild_required"])
        selected["tools"]["llvm_dir"] = "/changed"
        self.assertEqual(self.profile, before)

    def test_same_revision_needs_no_rebuild_recipe(self):
        self.profile.pop("maintenance")
        tested = self.commit(self.source, {"triton/cmake/llvm-hash.txt": "a" * 40})
        selected = self.select(tested)
        self.assertFalse(selected["llvm_selection"]["rebuild_required"])

    def test_absent_dependency_keeps_explicit_profile_for_frontend_fixture(self):
        tested = self.commit(self.source, {"README.md": "No Triton submodule in this fixture\n"})
        selected = self.select(tested)
        self.assertEqual(selected["llvm_revision"], self.profile["llvm_revision"])
        self.assertEqual(selected["llvm_selection"]["source"], "profile")

    def test_new_revision_cannot_use_a_candidate_recipe(self):
        self.profile.pop("maintenance")
        tested = self.commit(self.source, {"triton/cmake/llvm-hash.txt": "b" * 40,
                                          "Dockerfile": "FROM attacker-defined-recipe\n",
                                          "prepare_llvm.sh": "exit 0\n"})
        with self.assertRaisesRegex(EnvironmentSelectionError, "no trusted maintenance recipe"):
            self.select(tested)

    def test_malformed_llvm_is_rejected_before_any_build(self):
        for value in ("main", "a" * 41, "a" * 40 + "\nextra-command"):
            tested = self.commit(self.source, {"triton/cmake/llvm-hash.txt": value})
            with self.assertRaisesRegex(EnvironmentSelectionError, "40 lowercase hexadecimal"):
                self.select(tested)

    def test_gitlink_uses_exact_object_in_host_mirror_without_gitmodules(self):
        triton = self.repository("trusted-triton")
        frozen = self.commit(triton, {"cmake/llvm-hash.txt": "b" * 40})
        self.commit(triton, {"cmake/llvm-hash.txt": "c" * 40})
        mirror = self.root / "trusted-triton.git"
        subprocess.run(["git", "clone", "--bare", str(triton), str(mirror)], check=True, capture_output=True)
        self.commit(self.source, {".gitmodules": '[submodule "triton"]\npath = triton\nurl = forbidden-placeholder\n'})
        git(self.source, "update-index", "--add", "--cacheinfo", "160000," + frozen + ",triton")
        git(self.source, "commit", "-m", "pin trusted gitlink")
        self.profile["dependency_host_sources"] = {"triton": str(mirror)}
        selected = self.select(git(self.source, "rev-parse", "HEAD"))
        self.assertEqual(selected["llvm_revision"], "b" * 40)
        self.assertEqual(selected["llvm_selection"]["source_sha"], frozen)
        self.assertEqual(selected["llvm_selection"]["source"], "trusted_host_gitlink")

    def test_gitlink_without_trusted_source_cannot_fall_back_to_profile(self):
        tested = self.commit(self.source, {"README.md": "fixture\n"})
        git(self.source, "update-index", "--add", "--cacheinfo", "160000," + tested + ",triton")
        git(self.source, "commit", "-m", "unavailable gitlink")
        with self.assertRaisesRegex(EnvironmentSelectionError, "requires a trusted dependency source"):
            self.select(git(self.source, "rev-parse", "HEAD"))

    def test_recipe_cannot_point_into_task_workspace(self):
        self.config["workspace_host"] = str(self.root)
        tested = self.commit(self.source, {"triton/cmake/llvm-hash.txt": "b" * 40})
        with self.assertRaisesRegex(EnvironmentSelectionError, "task workspace"):
            self.select(tested)


if __name__ == "__main__":
    unittest.main()
