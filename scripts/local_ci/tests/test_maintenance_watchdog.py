"""External detection remains active without email; mail errors stay observable."""
import contextlib
import io
import json
import os
from pathlib import Path
import smtplib
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from scripts.local_ci.maintenance import notify, watchdog


class ExternalWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {'heartbeat_url': 'https://example.invalid/heartbeat.json',
                       'worker_id': 'worker-test', 'state_dir': str(self.root / 'state')}
        self.snapshot = {'schema': 'triton-anchor-local-ci-worker-health', 'worker_id': 'worker-test',
                         'heartbeat_at': time.time(), 'state': 'healthy', 'issues': []}
        self.smtp = {'host': 'smtp.example.invalid', 'from': 'ci@example.invalid',
                     'accounts': ['heron-mc'], 'account_emails': {'heron-mc': 'recipient@example.invalid'},
                     'starttls': True}

    def run_watchdog(self, unavailable=False):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        response = io.BytesIO(json.dumps(self.snapshot).encode())
        with patch.object(watchdog, 'urlopen', side_effect=URLError('offline') if unavailable else None,
                          return_value=response) as read, contextlib.redirect_stdout(io.StringIO()):
            code = watchdog.main(['--config', str(path)])
        read.assert_called_once()
        return code, json.loads((self.root / 'state/watchdog-latest.json').read_text())

    def test_no_smtp_reads_fresh_heartbeat_without_recording_mail_delivery(self):
        with patch.object(watchdog, 'Notifier') as notifier:
            code, result = self.run_watchdog()
        notifier.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(result['notification']['status'], 'not_configured')
        self.assertEqual([i['code'] for i in result['issues']], ['notification_not_configured'])
        self.assertFalse(list((self.root / 'state').glob('notify-*')))

    def test_actual_host_missing_mail_issue_is_retained_but_nonblocking(self):
        self.snapshot.update(state='degraded', issues=[{'code': 'notification_not_configured', 'message': 'SMTP host/from not configured'}])
        code, result = self.run_watchdog()
        self.assertEqual(code, 0)
        self.assertEqual(result['issues'], self.snapshot['issues'])

    def test_stale_wrong_worker_unreachable_and_operational_fault_still_fail(self):
        cases = [('host_offline', {'heartbeat_at': time.time() - 10000}, False),
                 ('heartbeat_invalid', {'worker_id': 'different'}, False),
                 ('heartbeat_unreachable', {}, True),
                 ('poller_stale', {'state': 'degraded', 'issues': [{'code': 'poller_stale', 'message': 'stopped'}]}, False)]
        original = dict(self.snapshot)
        for expected, changes, unavailable in cases:
            with self.subTest(expected=expected):
                self.snapshot = {**original, **changes}
                code, result = self.run_watchdog(unavailable)
                self.assertEqual(code, 1)
                self.assertIn(expected, [i['code'] for i in result['issues']])
                self.assertEqual(result['notification']['status'], 'not_configured')

    def test_partial_mail_or_missing_oauth_authorization_cannot_be_hidden(self):
        for smtp in ([], {'host': 'smtp.example.invalid'}, {**self.smtp, 'host': {}}, {**self.smtp, 'port': 0},
                     {**self.smtp, 'account_emails': {}},
                     {**self.smtp, 'oauth2': {'token_endpoint': 'https://example.invalid/token', 'client_id': ''}}):
            with self.subTest(smtp=smtp), patch.object(watchdog, 'Notifier') as notifier:
                self.config['smtp'] = smtp
                code, result = self.run_watchdog()
                notifier.assert_not_called()
                self.assertEqual(code, 1)
                self.assertEqual(result['notification']['status'], 'pending')
                self.assertIn('notification_configuration_error', [i['code'] for i in result['issues']])

    def test_orphaned_authentication_secret_is_partial_configuration(self):
        with patch.dict(os.environ, {'LOCAL_CI_SMTP_PASSWORD': 'example-password'}):
            code, result = self.run_watchdog()
        self.assertEqual(code, 1)
        self.assertEqual(result['notification']['status'], 'pending')

    def test_mail_delivery_deduplication_and_failed_recovery_preserve_exit_status(self):
        self.config['smtp'] = self.smtp
        self.snapshot.update(state='degraded', issues=[{'code': 'poller_stale', 'message': 'stopped'}])
        with patch.object(notify.smtplib, 'SMTP') as transport:
            client = transport.return_value.__enter__.return_value
            code, result = self.run_watchdog()
            self.assertEqual((code, result['notification']['status']), (1, 'sent'))
            self.assertEqual(client.send_message.call_count, 1)
            code, result = self.run_watchdog()
            self.assertEqual((code, result['notification']['status']), (1, 'unchanged'))
            self.assertEqual(client.send_message.call_count, 1)
            self.snapshot.update(state='healthy', issues=[])
            client.send_message.side_effect = smtplib.SMTPException('private-error-do-not-log')
            code, result = self.run_watchdog()
            self.assertEqual((code, result['notification']['status']), (1, 'pending'))
            self.assertNotIn('private-error-do-not-log', json.dumps(result))
            client.send_message.side_effect = None
            code, result = self.run_watchdog()
            self.assertEqual((code, result['notification']['status']), (0, 'sent'))

    def test_configured_smtp_authentication_failure_is_pending_and_not_delivered(self):
        self.config['smtp'] = self.smtp
        self.snapshot.update(state='degraded', issues=[{'code': 'poller_stale', 'message': 'stopped'}])
        with patch.dict(os.environ, {'LOCAL_CI_SMTP_USERNAME': 'example-user', 'LOCAL_CI_SMTP_PASSWORD': 'example-password'}), \
                patch.object(notify.smtplib, 'SMTP') as transport:
            client = transport.return_value.__enter__.return_value
            client.login.side_effect = smtplib.SMTPAuthenticationError(535, b'private authentication diagnostic')
            code, result = self.run_watchdog()
            client.send_message.assert_not_called()
        self.assertEqual((code, result['notification']['status']), (1, 'pending'))


class WatchdogWorkflowPreparation(unittest.TestCase):
    def test_actual_preparation_requires_only_heartbeat_and_keeps_partial_smtp(self):
        # Execute the trusted workflow's Python preparation in a temporary folder;
        # it has no network/API/SMTP effects and cannot send any message.
        import yaml
        root = Path(__file__).resolve().parents[3]
        workflow = yaml.safe_load((root / '.github/workflows/local-ci-watchdog.yml').read_text(encoding='utf-8'))
        step = next(s for s in workflow['jobs']['check-heartbeat']['steps'] if s.get('name') == 'Prepare explicitly configured watchdog')
        script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for extra in ({}, {'SMTP_HOST': 'smtp.example.invalid'}, {'SMTP_AUTH_MODE': 'oauth2'}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                env = {key: '' for key in step['env']}
                env.update(WATCHDOG_URL='https://example.invalid/heartbeat.json', WATCHDOG_WORKER='worker-test',
                           WATCHDOG_MAX_AGE='1800', SMTP_AUTH_MODE='password', RUNNER_TEMP=directory)
                env.update(extra)
                with patch.dict(os.environ, env, clear=True):
                    exec(compile(script, 'watchdog-workflow-prepare', 'exec'), {})
                config = json.loads((Path(directory) / 'watchdog.json').read_text())
                self.assertEqual(config['worker_id'], 'worker-test')
                self.assertEqual(bool(config['smtp']), bool(extra))


if __name__ == '__main__':
    unittest.main()
