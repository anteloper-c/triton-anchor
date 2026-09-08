"""Frozen source manifests must include Git entries that archives leave empty."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.relay import Relay


class FrozenSourceReviewTests(unittest.TestCase):
    def test_checkout_records_exact_gitlink_in_addition_to_regular_files(self):
        with tempfile.TemporaryDirectory(prefix="ci-source-review-") as temporary:
            root = Path(temporary)
            repo = root / "fixture"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

            git("init", "-q")
            git("config", "user.name", "Local test")
            git("config", "user.email", "local-test@localhost")
            git("config", "core.autocrlf", "false")
            (repo / "README.md").write_text("local source fixture\n", encoding="utf-8")
            git("add", "README.md")
            dependency_sha = "a" * 40
            git("update-index", "--add", "--cacheinfo", "160000," + dependency_sha + ",FlagGems")
            git("commit", "-qm", "test frozen dependency")
            sha = git("rev-parse", "HEAD")
            relay = Relay({"url": str(repo)}, root / "state")
            relay.fetch()
            snapshot = relay.checkout(sha, root / "checkout")
            self.assertEqual(snapshot["FlagGems"], "gitlink:" + dependency_sha)
            self.assertIn("README.md", snapshot)
            self.assertTrue((root / "checkout/FlagGems").is_dir())
            # Rename detection must retain both sides for minimum-coverage policy.
            (repo / 'csrc').mkdir()
            git('mv', 'README.md', 'csrc/Lower.cpp')
            git('commit', '-qm', 'test compiler file')
            base = git('rev-parse', 'HEAD')
            git('mv', 'csrc/Lower.cpp', 'README.md')
            git('commit', '-qm', 'test compiler-to-doc rename')
            head = git('rev-parse', 'HEAD')
            relay.fetch()
            self.assertEqual(set(relay.changed_paths({'base_sha': base, 'tested_sha': head})),
                             {'csrc/Lower.cpp', 'README.md'})


if __name__ == "__main__":
    unittest.main()
