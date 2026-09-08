"""A matching host HEAD cannot authorize stale or writable container tools."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


if __name__ == '__main__':
    unittest.main()
