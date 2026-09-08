"""OAuth SMTP tests use fake HTTP/SMTP peers; never contact a real mailbox."""
import io
import json
import os
import smtplib
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from scripts.local_ci.maintenance.notify import refresh_access_token, send_email, validate_authentication


class OAuthSMTP(unittest.TestCase):
    def setUp(self):
        self.config = {'host': 'smtp.example.invalid', 'port': 587, 'starttls': True,
            'from': 'sender@example.invalid', 'accounts': ['receiver'],
            'account_emails': {'receiver': 'receiver@example.invalid'},
            'oauth2': {'token_endpoint': 'https://identity.example.invalid/token',
                       'client_id': 'test-client', 'refresh_token_env': 'TEST_REFRESH'}}
        self.env = patch.dict(os.environ, {'TEST_REFRESH': 'private-refresh'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, **extra):
        return io.BytesIO(json.dumps({'token_type': 'Bearer', 'access_token': 'private-access',
                                     'expires_in': 3600, **extra}).encode())

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    @patch('scripts.local_ci.maintenance.notify.smtplib.SMTP')
    def test_success_refresh_and_starttls_xoauth2(self, smtp, http):
        http.return_value = self.response()
        client = smtp.return_value.__enter__.return_value
        send_email(self.config, 'incident', 'test only')
        self.assertEqual(http.call_args.args[0].full_url, self.config['oauth2']['token_endpoint'])
        client.starttls.assert_called_once()
        client.ehlo.assert_called_once()
        self.assertEqual(client.auth.call_args.args[0], 'XOAUTH2')
        self.assertIn('auth=Bearer private-access', client.auth.call_args.args[1]())
        self.assertEqual(client.auth.call_args.args[1](b'challenge'), '')
        client.login.assert_not_called()
        client.send_message.assert_called_once()

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    @patch('scripts.local_ci.maintenance.notify.smtplib.SMTP_SSL')
    def test_supplied_access_token_uses_ssl_without_refresh(self, smtp, http):
        self.config.update(ssl=True, port=465)
        self.config['oauth2'] = {'access_token_env': 'TEST_ACCESS'}
        os.environ['TEST_ACCESS'] = 'private-access'
        send_email(self.config, 'incident', 'test only')
        http.assert_not_called()
        client = smtp.return_value.__enter__.return_value
        client.starttls.assert_not_called()
        client.auth.assert_called_once()

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    @patch('scripts.local_ci.maintenance.notify.smtplib.SMTP')
    def test_tls_is_required_before_any_network(self, smtp, http):
        self.config['starttls'] = False
        with self.assertRaisesRegex(ValueError, 'requires SSL or STARTTLS'):
            send_email(self.config, 'incident', 'test only')
        smtp.assert_not_called()
        http.assert_not_called()

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    def test_refresh_failures_are_sanitized(self, http):
        errors = [HTTPError('https://identity.example.invalid/private-refresh', 400, 'private-refresh', {}, None),
                  ValueError('private-access')]
        for failure in errors:
            with self.subTest(error=type(failure).__name__):
                http.side_effect = failure
                with self.assertRaises(ValueError) as caught:
                    refresh_access_token(self.config['oauth2'], 'private-refresh')
                self.assertNotIn('private-', str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    def test_missing_client_and_http_endpoint_fail_closed(self, http):
        for settings in ({}, {'client_id': 'x', 'token_endpoint': 'http://identity.example.invalid/token'}):
            with self.assertRaises(ValueError):
                refresh_access_token(settings, 'private-refresh')
        http.assert_not_called()

    def test_missing_registration_is_visible_without_refresh(self):
        self.config['oauth2']['client_id'] = ''
        with self.assertRaisesRegex(ValueError, 'not configured'):
            validate_authentication(self.config)

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    def test_refresh_returns_rotated_token_for_managed_cache(self, http):
        http.return_value = self.response(refresh_token='rotated-private-refresh')
        result = refresh_access_token(self.config['oauth2'], 'private-refresh')
        self.assertEqual(result['refresh_token'], 'rotated-private-refresh')
        self.assertEqual(result['expires_in'], 3600)

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    def test_invalid_rotated_refresh_token_is_rejected(self, http):
        for token in (None, '', 123, 'bad\nvalue', 'contains space', 'x' * 65537):
            with self.subTest(value_type=type(token).__name__):
                http.return_value = self.response(refresh_token=token)
                with self.assertRaisesRegex(ValueError, 'token refresh failed'):
                    refresh_access_token(self.config['oauth2'], 'private-refresh')

    @patch('scripts.local_ci.maintenance.notify.request.urlopen')
    @patch('scripts.local_ci.maintenance.notify.smtplib.SMTP')
    def test_smtp_rejection_is_sanitized_and_does_not_send(self, smtp, http):
        http.return_value = self.response()
        client = smtp.return_value.__enter__.return_value
        client.auth.side_effect = smtplib.SMTPAuthenticationError(535, b'private-access')
        with self.assertRaises(ValueError) as caught:
            send_email(self.config, 'incident', 'test only')
        self.assertNotIn('private-access', str(caught.exception))
        client.send_message.assert_not_called()

    @patch('scripts.local_ci.maintenance.notify.smtplib.SMTP')
    def test_password_path_remains_supported(self, smtp):
        self.config.pop('oauth2')
        os.environ.update(LOCAL_CI_SMTP_USERNAME='sender@example.invalid', LOCAL_CI_SMTP_PASSWORD='password-test')
        send_email(self.config, 'incident', 'test only')
        client = smtp.return_value.__enter__.return_value
        client.login.assert_called_once_with('sender@example.invalid', 'password-test')
        client.auth.assert_not_called()


if __name__ == '__main__':
    unittest.main()
