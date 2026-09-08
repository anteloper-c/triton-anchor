"""Actual local bare-Git publication and loopback SMTP delivery fault tests."""
import json
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest

from scripts.local_ci.maintenance.notify import Notifier
from scripts.local_ci.maintenance.publish_health import HealthPublisher, PublicationError
from scripts.local_ci.maintenance.watchdog import evaluate
from scripts.local_ci.maintenance.workers import atomic_json


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), '-c', 'core.hooksPath=', *args],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'local fixture git {args[0]} failed: {result.stderr}')
    return result.stdout.strip()


def snapshot(now=None):
    return {'schema': 'triton-anchor-local-ci-worker-health/v2', 'worker_id': 'acceptance-worker',
            'heartbeat_at': now or time.time(), 'state': 'degraded', 'workers': [],
            'issues': [{'code': 'poller_stale', 'message': '本机故障注入：Poller 心跳过期。'}]}


class SMTPSink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reject_first=False):
        self.messages, self.envelopes = [], []
        self.attempts = 0
        self.reject_first = reject_first
        outer = self
        class Handler(socketserver.StreamRequestHandler):
            def reply(self, text):
                self.wfile.write((text + '\r\n').encode())
                self.wfile.flush()

            def handle(self):
                self.connection.settimeout(10)
                self.reply('220 loopback CI test sink')
                recipients = []
                while True:
                    line = self.rfile.readline(65536)
                    if not line:
                        return
                    command = line.decode('utf-8', errors='replace').strip()
                    if command.upper().startswith(('EHLO ', 'HELO ')):
                        self.reply('250-loopback')
                        self.reply('250 SIZE 1000000')
                    elif command.upper().startswith('MAIL FROM:'):
                        recipients = []
                        self.reply('250 sender accepted')
                    elif command.upper().startswith('RCPT TO:'):
                        recipients.append(command[8:])
                        self.reply('250 recipient accepted')
                    elif command.upper() == 'DATA':
                        self.reply('354 end with a single dot')
                        lines = []
                        while True:
                            item = self.rfile.readline(65536)
                            if not item or item == b'.\r\n':
                                break
                            lines.append(item[1:] if item.startswith(b'..') else item)
                        outer.attempts += 1
                        if outer.reject_first and outer.attempts == 1:
                            self.reply('451 injected temporary delivery failure')
                        else:
                            outer.messages.append(b''.join(lines))
                            outer.envelopes.append(recipients[:])
                            self.reply('250 queued in local memory only')
                    elif command.upper() == 'QUIT':
                        self.reply('221 bye')
                        return
                    elif command.upper() in ('RSET', 'NOOP'):
                        self.reply('250 ok')
                    else:
                        self.reply('502 unsupported fixture command')
        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def finish(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def local_smtp_config(sink):
    # Synthetic reserved-domain addresses are generated only for the local sink.
    # Real recipient addresses must remain in operator config or GitHub Secrets.
    accounts = ['heron-mc', 'likehupochuan']
    domain = 'example.invalid'
    return {'accounts': accounts, 'account_emails': {a: a + '@' + domain for a in accounts},
            'host': '127.0.0.1', 'port': sink.server_address[1], 'from': 'ci-sink@' + domain,
            'starttls': False, 'username_env': 'CI_TEST_UNUSED_SMTP_USER'}


def local_delivery_acceptance(root):
    """Leave reviewable local Git/snapshot/synthetic-SMTP evidence in root."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    remote = root / 'relay.git'
    if not remote.exists():
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    state = root / 'state'
    config = {'worker_id': 'acceptance-worker', 'state_dir': str(state),
              'relay': {'url': str(remote.resolve()), 'results_branch': 'local-ci-results'}}
    atomic_json(state / 'health/latest.json', snapshot())
    publication = HealthPublisher(config).publish()
    sink = SMTPSink(reject_first=True)
    try:
        config['smtp'] = local_smtp_config(sink)
        atomic_json(root / 'local-config.json', config)
        notifier = Notifier(state / 'notify', config['smtp'])
        published = json.loads(git(remote, 'show', 'local-ci-results:health/acceptance-worker.json'))
        issues = evaluate(published, 'acceptance-worker')
        delivery = [notifier.update('acceptance-worker', issues)['status'],
                    notifier.update('acceptance-worker', issues)['status'],
                    notifier.update('acceptance-worker', issues)['status']]
        recovered = {**snapshot(), 'state': 'healthy', 'issues': []}
        atomic_json(state / 'health/latest.json', recovered)
        HealthPublisher(config).publish()
        recovered_publication = json.loads(git(remote, 'show', 'local-ci-results:health/acceptance-worker.json'))
        delivery.append(notifier.update('acceptance-worker', evaluate(recovered_publication, 'acceptance-worker'))['status'])
        for number, message in enumerate(sink.messages, 1):
            (root / f'loopback-message-{number}.eml').write_bytes(message)
        result = {'git_publication': publication['status'], 'delivery_sequence': delivery,
                  'smtp_attempts': sink.attempts, 'messages_received': len(sink.messages),
                  'all_envelopes_have_two_recipients': all(len(targets) == 2 for targets in sink.envelopes),
                  'chain': 'health snapshot -> local Git -> watchdog evaluation -> loopback SMTP',
                  'network_scope': 'loopback SMTP and local bare Git only'}
        atomic_json(root / 'acceptance-result.json', result)
        return result
    finally:
        sink.finish()


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

    def test_loopback_smtp_retry_deduplication_and_recovery(self):
        result = local_delivery_acceptance(self.root / 'smtp-acceptance')
        self.assertEqual(result['delivery_sequence'], ['pending', 'sent', 'unchanged', 'sent'])
        self.assertEqual(result['smtp_attempts'], 3)
        self.assertEqual(result['messages_received'], 2)
        self.assertTrue(result['all_envelopes_have_two_recipients'])


if __name__ == '__main__':
    unittest.main()
