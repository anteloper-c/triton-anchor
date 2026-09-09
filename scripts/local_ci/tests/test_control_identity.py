"""Essential behavior checks for related CI responsibilities."""
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

from runtime import control

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

import copy

from runtime.control import verify_container_control

class ContainerControlTests(unittest.TestCase):
    def setUp(self):
        self.files = {'runtime/engine.py': 'a' * 64, 'tools/run_tool.py': 'b' * 64}
        self.identity = {'verified': False, 'files': {'scripts/local_ci/' + name: digest for name, digest in self.files.items()}}
        self.profile = {'container': {'name': 'fixed-worker'}}
        self.inspect = {'Id': 'c' * 64, 'State': {'Running': True},
                        'Mounts': [{'Destination': '/opt/anchor-ci', 'RW': False, 'Type': 'bind'}]}
        self.observed = {'files': self.files, 'mount_read_only': True}
        self.calls = []

    def fake_process(self, command, **kwargs):
        self.calls.append(command)
        value = [self.inspect] if command[1] == 'inspect' else self.observed
        return subprocess.CompletedProcess(command, 0, json.dumps(value), '')

    def verify(self):
        return verify_container_control({'local_acceptance': True}, self.profile, self.identity, run=self.fake_process)

    def test_matching_read_only_mount_passes_even_for_local_scope(self):
        result = self.verify()
        self.assertTrue(result['verified'])
        self.assertTrue(result['mount_read_only'])
        packed = json.dumps(self.files, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(result['tree_sha256'], hashlib.sha256(packed).hexdigest())
        self.assertIn('-I', self.calls[1])
        self.assertIn('-c', self.calls[1])

    def test_writable_mount_fails_before_executing_container_code(self):
        self.inspect['Mounts'][0]['RW'] = True
        with self.assertRaises(ValueError):
            self.verify()
        self.assertEqual(len(self.calls), 1)

    def test_writable_nested_mount_is_rejected(self):
        self.inspect['Mounts'].append({'Destination': '/opt/anchor-ci/runtime', 'RW': True, 'Type': 'volume'})
        with self.assertRaises(ValueError):
            self.verify()

    def test_old_extra_or_missing_control_bytes_are_rejected(self):
        for files in ({'runtime/engine.py': 'd' * 64}, dict(self.files, extra='e' * 64), {}):
            self.observed = {'files': files, 'mount_read_only': True}
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.verify()

    def test_actual_mount_read_only_report_is_required(self):
        self.observed = copy.deepcopy(self.observed)
        self.observed['mount_read_only'] = False
        with self.assertRaises(ValueError):
            self.verify()

    def test_image_copy_without_explicit_mount_is_rejected(self):
        self.inspect['Mounts'] = []
        with self.assertRaises(ValueError):
            self.verify()

from unittest import mock

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
