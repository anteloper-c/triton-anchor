"""Real process cleanup tests; Linux-only cases require a worker PID namespace.

Set LOCAL_CI_PROCESS_CLEANUP_TEST=1 only in an idle acceptance worker to exercise
the root sweep of dedicated uid 1000/1001; never set this on a general host.
"""
import base64
import hashlib
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

    def wait_registered(self, pid):
        """Child stdout precedes durable launcher registration; wait for identity."""
        record = Path('/tmp') / f'anchor-ci-processes-{os.geteuid()}' / (self.ident + '.json')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if record.is_file():
                identity = json.loads(record.read_text())
                self.assertEqual(identity['pid'], pid)
                self.assertEqual(identity['uid'], os.geteuid())
                self.assertEqual(identity['pgrp'], pid)
                return
            time.sleep(0.01)
        self.fail('launcher did not durably register the child identity')

    def cleanup_launcher(self, launched, pid=None):
        """Failure cleanup is limited to this test's still-running launcher."""
        if launched.poll() is None:
            if pid is not None:
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            launched.kill()
        launched.communicate(timeout=5)

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
        pid = int(launched.stdout.readline().strip())
        self.addCleanup(self.cleanup_launcher, launched, pid)
        self.wait_registered(pid)
        started = time.monotonic()
        stopped = subprocess.run(self.command('stop', payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertLess(time.monotonic() - started, 8)
        launched.communicate(timeout=5)
        self.assert_dead(pid)

    def test_stop_during_registration_waits_for_identity_then_kills_child(self):
        # Hold the real launcher immediately before atomic publication. This
        # deterministically exercises the race observed on GitHub's fast runner.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entered, release = root / 'entered', root / 'release'
            wrapper = """import importlib.util,pathlib,sys,time
spec=importlib.util.spec_from_file_location('launcher_under_test',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original=module.atomic_pidfile
def delayed(path,value):
 if path.suffix == '.json':
  pathlib.Path(sys.argv[3]).write_text('registration entered')
  deadline=time.monotonic()+5
  while not pathlib.Path(sys.argv[4]).exists():
   if time.monotonic()>deadline: raise RuntimeError('test registration gate timed out')
   time.sleep(0.01)
 return original(path,value)
module.atomic_pidfile=delayed
raise SystemExit(module.main(['run',sys.argv[2]]))
"""
            child = 'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)'
            payload = self.spec([sys.executable, '-c', child])
            launched = subprocess.Popen([sys.executable, '-I', '-c', wrapper, str(HELPER), payload,
                                         str(entered), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pid = int(launched.stdout.readline().strip())
            self.addCleanup(self.cleanup_launcher, launched, pid)
            deadline = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(entered.exists())
            stopped = subprocess.Popen(self.command('stop', payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.cleanup_launcher, stopped)
            try:
                with self.assertRaises(subprocess.TimeoutExpired,
                                       msg='stop must not claim completion before startup registration finishes'):
                    stopped.communicate(timeout=0.2)
            finally:
                release.write_text('release registration')
            _, stderr = stopped.communicate(timeout=10)
            self.assertEqual(stopped.returncode, 0, stderr)
            launched.communicate(timeout=5)
            self.assert_dead(pid)

    def test_stop_before_launcher_start_prevents_later_child_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'must-not-execute'
            payload = self.spec([sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text("executed")'])
            stopped = subprocess.run(self.command('stop', payload), capture_output=True, text=True, timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            launched = subprocess.run(self.command('run', payload), capture_output=True, text=True, timeout=5)
            self.assertNotEqual(launched.returncode, 0)
            self.assertFalse(marker.exists())

    def test_stale_stop_does_not_cancel_recovered_invocation_with_same_id(self):
        old_payload = self.spec([sys.executable, '-c', 'raise SystemExit(0)'])
        subprocess.run(self.command('stop', old_payload), check=True, capture_output=True, timeout=10)
        child = 'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)'
        payload = self.spec([sys.executable, '-c', child])
        launched = subprocess.Popen(self.command('run', payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pid = int(launched.stdout.readline().strip())
        self.addCleanup(self.cleanup_launcher, launched, pid)
        self.wait_registered(pid)
        subprocess.run(self.command('stop', old_payload), check=True, capture_output=True, timeout=10)
        self.assertIsNone(launched.poll())
        stopped = subprocess.run(self.command('stop', payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        launched.communicate(timeout=5)
        self.assert_dead(pid)

    def test_registration_metadata_rejects_symlinks_and_directories(self):
        folder = Path('/tmp') / f'anchor-ci-processes-{os.geteuid()}'
        folder.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            victim = Path(temporary) / 'unrelated'
            victim.write_text('must not change')
            for field in ('lock', 'cancelled', 'json'):
                for kind in ('symlink', 'directory'):
                    with self.subTest(field=field, kind=kind):
                        self.ident = 'lifecycle-test-' + uuid.uuid4().hex
                        payload = self.spec([sys.executable, '-c', 'raise SystemExit(0)'])
                        spec = json.loads(base64.urlsafe_b64decode(payload))
                        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'),
                                                                ensure_ascii=False).encode()).hexdigest()
                        path = folder / ((fingerprint if field == 'cancelled' else self.ident) + '.' + field)
                        try:
                            path.symlink_to(victim) if kind == 'symlink' else path.mkdir()
                            stopped = subprocess.run(self.command('stop', payload), capture_output=True, text=True, timeout=10)
                            self.assertNotEqual(stopped.returncode, 0)
                            self.assertEqual(victim.read_text(), 'must not change')
                        finally:
                            path.unlink() if path.is_symlink() else path.rmdir()

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
