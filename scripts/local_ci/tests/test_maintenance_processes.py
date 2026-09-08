"""Real process cleanup tests; Linux-only cases require a worker PID namespace.

Set LOCAL_CI_PROCESS_CLEANUP_TEST=1 only in an idle acceptance worker to exercise
the root sweep of dedicated uid 1000/1001; never set this on a general host.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
COMMON_SPEC = importlib.util.spec_from_file_location('process_test_common', ROOT / 'runtime/common.py')
common = importlib.util.module_from_spec(COMMON_SPEC)
COMMON_SPEC.loader.exec_module(common)
HELPER = ROOT / 'runtime/container_process.py'


class HostProcessCleanup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'process.log'
        self.processes = []
        original = subprocess.Popen
        def record(*args, **kwargs):
            proc = original(*args, **kwargs)
            self.processes.append(proc)
            return proc
        self.patcher = patch.object(common.subprocess, 'Popen', side_effect=record)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_termination_callback_failure_still_reaps_host_client(self):
        def fail():
            raise RuntimeError('injected remote cleanup failure')
        with self.assertRaisesRegex(RuntimeError, 'remote cleanup failure'):
            common.execute([sys.executable, '-c', 'import time; time.sleep(60)'], self.log,
                           cancelled=lambda: True, terminate=fail)
        self.assertIsNotNone(self.processes[0].poll())

    def test_cancellation_probe_failure_still_stops_and_reaps(self):
        calls = []
        def fail():
            raise RuntimeError('injected monitor failure')
        with self.assertRaisesRegex(RuntimeError, 'monitor failure'):
            common.execute([sys.executable, '-c', 'import time; time.sleep(60)'], self.log,
                           cancelled=fail, terminate=lambda: calls.append('stop'))
        self.assertEqual(calls, ['stop'])
        self.assertIsNotNone(self.processes[0].poll())

    def test_timeout_returns_distinct_reason(self):
        result = common.execute([sys.executable, '-c', 'import time; time.sleep(60)'],
                                self.log, timeout=0.05)
        self.assertEqual(result['termination'], 'timeout')
        self.assertIsNotNone(self.processes[0].poll())


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc').is_dir(), 'Linux worker process test')
class LinuxProcessCleanup(unittest.TestCase):
    def setUp(self):
        self.ident = 'lifecycle-test-' + uuid.uuid4().hex
        self.payload = None

    def spec(self, argv):
        data = {'id': self.ident, 'argv': argv, 'cwd': '/tmp', 'env': {}}
        self.payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return self.payload

    def command(self, operation, payload=None):
        return [sys.executable, '-I', str(HELPER), operation, *([payload] if payload else [])]

    def assert_dead(self, pid):
        path = Path('/proc') / str(pid) / 'stat'
        if path.exists():
            data = path.read_text()
            self.assertEqual(data[data.rfind(')') + 2:].split()[0], 'Z')

    def test_sigterm_ignoring_process_is_killed_and_launcher_reaped(self):
        code = 'import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)'
        payload = self.spec([sys.executable, '-c', code])
        launched = subprocess.Popen(self.command('run', payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: launched.kill() if launched.poll() is None else None)
        pid = int(launched.stdout.readline().strip())
        started = time.monotonic()
        stopped = subprocess.run(self.command('stop', payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertLess(time.monotonic() - started, 8)
        launched.communicate(timeout=5)
        self.assert_dead(pid)

    def test_normal_exit_stops_background_group_member(self):
        child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
        code = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",' + repr(child) + '],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)'
        result = subprocess.run(self.command('run', self.spec([sys.executable, '-c', code])),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_dead(int(result.stdout.strip()))

    @unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0 and os.environ.get('LOCAL_CI_PROCESS_CLEANUP_TEST') == '1', 'requires an explicitly reserved idle acceptance worker')
    def test_root_sweep_catches_setsid_escape_and_uid_directories_are_separate(self):
        for uid in (1000, 1001):
            payload = self.spec([sys.executable, '-c', 'import os; print(os.getuid())'])
            result = subprocess.run(self.command('run', payload), user=uid, group=1000,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(uid))
            folder = Path('/tmp') / f'anchor-ci-processes-{uid}'
            self.assertEqual(folder.stat().st_uid, uid)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        denied = subprocess.run(self.command('clean-users'), user=1000, group=1000, capture_output=True, timeout=5)
        self.assertNotEqual(denied.returncode, 0)
        child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
        code = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)'
        escaped = subprocess.run([sys.executable, '-c', code], user=1000, group=1000,
                                 capture_output=True, text=True, timeout=5)
        pid = int(escaped.stdout.strip())
        cleaned = subprocess.run(self.command('clean-users'), capture_output=True, text=True, timeout=12)
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assert_dead(pid)


if __name__ == '__main__':
    unittest.main()
