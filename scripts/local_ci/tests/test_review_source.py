"""Frozen source manifests must include Git entries that archives leave empty."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.relay import Relay
from runtime.common import digest
from runtime.report import build_source_index, validate_architecture_evidence, validate_source_index


class FrozenSourceReviewTests(unittest.TestCase):
    def test_index_keeps_frozen_lines_after_candidate_is_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / 'candidate.py'
            candidate.write_text('original\n')
            manifest = {'candidate.py': digest(candidate)}
            index = build_source_index(root, manifest)
            candidate.write_text('replacement\nsecond\nthird\n')
            evidence = [{'path': 'candidate.py', 'line': 1, 'reason': 'Reviewed original source'}]
            # Neither the model-selected file nor an outside symlink target may
            # be opened during either path or line validation.
            forbidden = mock.Mock(side_effect=AssertionError('No source filesystem access after checkout'))
            with mock.patch.multiple(Path, open=forbidden, read_text=forbidden, read_bytes=forbidden,
                                     resolve=forbidden, is_file=forbidden):
                validate_architecture_evidence(evidence, index)
                evidence[0]['line'] = 2
                with self.assertRaisesRegex(ValueError, 'line'):
                    validate_architecture_evidence(evidence, index)
            forbidden.assert_not_called()
            self.assertEqual(index['files'], {'candidate.py': 1})

    def test_index_rejects_changed_initial_bytes_and_omits_nonordinary_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / 'candidate.py'
            candidate.write_text('original\n')
            manifest = {'candidate.py': digest(candidate), 'backend': 'gitlink:' + 'a' * 40,
                        'linked.py': 'symlink:outside.py'}
            index = build_source_index(root, manifest)
            self.assertEqual(index['files'], {'candidate.py': 1})
            for path in ('backend/impl.py', 'linked.py', '../outside.py', '/etc/passwd', 'C:/outside.py'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    validate_architecture_evidence([{'path': path, 'reason': 'Claim'}], index)
            candidate.write_text('changed\n')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                build_source_index(root, manifest)

    def test_missing_malformed_or_differently_bound_index_fails_closed(self):
        manifest = {'candidate.py': 'a' * 64}
        for index in (None, {}, {'manifest_sha256': 'a' * 64, 'files': {'candidate.py': True}},
                      {'manifest_sha256': 'a' * 64, 'files': {'../outside': 1}},
                      {'manifest_sha256': 'a' * 64, 'files': {'candidate.py': 1}}):
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_source_index(index, manifest)

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
