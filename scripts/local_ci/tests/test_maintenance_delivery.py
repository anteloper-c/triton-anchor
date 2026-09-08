"""Actual local bare-Git health publication and transport failure tests."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from scripts.local_ci.maintenance.publish_health import HealthPublisher, PublicationError
from scripts.local_ci.maintenance.workers import atomic_json


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), '-c', 'core.hooksPath=', *args],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'local fixture git {args[0]} failed: {result.stderr}')
    return result.stdout.strip()


def snapshot(now=None):
    return {'schema': 'triton-anchor-local-ci-worker-health', 'worker_id': 'acceptance-worker',
            'heartbeat_at': now or time.time(), 'state': 'degraded', 'workers': [],
            'issues': [{'code': 'poller_stale', 'message': '本机故障注入：Poller 心跳过期。'}]}


class HealthDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'relay.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        self.config = {'worker_id': 'acceptance-worker', 'state_dir': str(self.root / 'state'),
                       'relay': {'url': str(self.remote), 'results_branch': 'local-ci-results'}}
        self.source = self.root / 'state/health/latest.json'
        atomic_json(self.source, snapshot(1000))

    def test_receive_hook_remains_active_and_failed_delivery_can_retry(self):
        publisher = HealthPublisher(self.config)
        publisher.publish()
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8', newline='\n')
        hook.chmod(0o755)
        changed = snapshot(2000)
        atomic_json(self.source, changed)
        with self.assertRaisesRegex(PublicationError, 'publication failed'):
            publisher.publish()
        self.assertEqual(json.loads(git(self.remote, 'show', 'local-ci-results:health/acceptance-worker.json'))['heartbeat_at'], 1000)
        hook.unlink()
        publisher.publish()
        self.assertEqual(json.loads(git(self.remote, 'show', 'local-ci-results:health/acceptance-worker.json')), changed)

    def test_publishes_real_snapshot_to_local_bare_git_without_task_checkout(self):
        result = HealthPublisher(self.config).publish()
        self.assertEqual(result['status'], 'published')
        published = json.loads(git(self.remote, 'show', 'local-ci-results:health/acceptance-worker.json'))
        self.assertEqual(published['heartbeat_at'], 1000)
        self.assertFalse((self.root / 'state/relay').exists())

    def test_real_non_fast_forward_retries_preserve_concurrent_task_results(self):
        publisher = HealthPublisher(self.config)
        publisher.publish()
        other = self.root / 'task-publisher'
        subprocess.run(['git', 'clone', '--branch', 'local-ci-results', str(self.remote), str(other)], check=True, capture_output=True)
        git(other, 'config', 'user.name', 'Fixture Task Publisher')
        git(other, 'config', 'user.email', 'fixture@localhost')
        atomic_json(other / 'runs/task/run/result.json', {'fixture': 'concurrent task publication'})
        git(other, 'add', '.')
        git(other, 'commit', '-m', 'concurrent task fixture')
        atomic_json(self.source, snapshot(2000))
        pushed = []
        def inject(argv, **kwargs):
            if 'push' in argv and not pushed:
                pushed.append(True)
                git(other, 'push', 'origin', 'HEAD:local-ci-results')
            return subprocess.run(argv, **kwargs)
        self.assertEqual(HealthPublisher(self.config, run=inject).publish()['status'], 'published')
        self.assertIn('concurrent task publication', git(self.remote, 'show', 'local-ci-results:runs/task/run/result.json'))
        self.assertEqual(json.loads(git(self.remote, 'show', 'local-ci-results:health/acceptance-worker.json'))['heartbeat_at'], 2000)

    def test_failed_push_is_bounded_and_retains_snapshot(self):
        publisher = HealthPublisher(self.config)
        publisher.publish()
        atomic_json(self.source, snapshot(2000))
        calls = []
        def fail(argv, **kwargs):
            if 'push' in argv:
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, '', 'injected failure')
            return subprocess.run(argv, **kwargs)
        with self.assertRaisesRegex(PublicationError, 'retained for retry'):
            HealthPublisher(self.config, run=fail).publish()
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.source.read_text())['heartbeat_at'], 2000)
        self.assertEqual(publisher.publish()['status'], 'published')

    def test_identity_mismatch_does_not_write_remote(self):
        value = snapshot()
        value['worker_id'] = 'different-worker'
        atomic_json(self.source, value)
        with self.assertRaisesRegex(PublicationError, 'identity'):
            HealthPublisher(self.config).publish()
        self.assertEqual(git(self.remote, 'for-each-ref', '--format=%(refname)'), '')



if __name__ == '__main__':
    unittest.main()
