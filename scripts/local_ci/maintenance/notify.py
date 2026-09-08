"""SMTP incident/recovery notifications with durable deduplication and retries."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import parseaddr
from http.client import HTTPException
import hashlib
import json
import os
from pathlib import Path
import smtplib
import ssl
import time
from urllib import error, parse, request

from .workers import atomic_json, file_lock, read_json


def refresh_access_token(settings, refresh_token, *, opener=None):
    """Exchange a trusted OAuth refresh token; never expose response diagnostics."""
    endpoint = settings.get('token_endpoint', '')
    parsed = parse.urlsplit(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not settings.get('client_id') or not refresh_token):
        raise ValueError('OAuth2 HTTPS endpoint, client_id and refresh token must be configured')
    fields = {'grant_type': 'refresh_token', 'client_id': settings['client_id'], 'refresh_token': refresh_token}
    if settings.get('scope'):
        fields['scope'] = settings['scope']
    if settings.get('client_secret_env'):
        secret = os.environ.get(settings['client_secret_env'], '')
        if not secret:
            raise ValueError('OAuth2 client secret is not configured')
        fields['client_secret'] = secret
    call = request.Request(endpoint, data=parse.urlencode(fields).encode('utf-8'),
                           headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with (opener.open if opener else request.urlopen)(call, timeout=30) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError('oversized token response')
        document = json.loads(raw)
        token = document.get('access_token')
        if (document.get('token_type', '').lower() != 'bearer' or not isinstance(token, str)
                or not token or len(token) > 65536 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError('invalid token response')
        rotated = document.get('refresh_token', refresh_token)
        if (not isinstance(rotated, str) or not rotated or len(rotated) > 65536
                or any(ord(c) < 33 or ord(c) > 126 for c in rotated)):
            raise ValueError('invalid rotated refresh token')
        return {'access_token': token, 'refresh_token': rotated,
                'expires_in': max(1, min(86400, int(document.get('expires_in', 3600))))}
    except (OSError, error.URLError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('OAuth2 token refresh failed; check configuration or renew authorization') from None


def oauth_access_token(settings):
    supplied = os.environ.get(settings.get('access_token_env', ''), '')
    if supplied:
        if len(supplied) > 65536 or any(ord(c) < 33 or ord(c) > 126 for c in supplied):
            raise ValueError('OAuth2 access token is invalid')
        return supplied
    refresh = os.environ.get(settings.get('refresh_token_env', ''), '')
    return refresh_access_token(settings, refresh)['access_token']


def validate_authentication(config):
    """Report missing OAuth setup without contacting the identity provider."""
    settings = config.get('oauth2')
    if settings is None:
        return
    if not isinstance(settings, dict):
        raise ValueError('OAuth2 settings must be an object')
    if not config.get('ssl', False) and not config.get('starttls', True):
        raise ValueError('OAuth2 SMTP requires SSL or STARTTLS')
    if os.environ.get(settings.get('access_token_env', ''), ''):
        return
    if (not settings.get('client_id') or not settings.get('token_endpoint', '').startswith('https://')
            or not os.environ.get(settings.get('refresh_token_env', ''), '')):
        raise ValueError('OAuth2 application or user authorization is not configured')


def recipients(config):
    """Resolve the requested Gitee accounts using explicit operator mappings.

    Gitee usernames are not email addresses. No guessed or scraped private email
    is used; an unresolved account is a visible configuration error.
    """
    mapping = config.get("account_emails", {})
    accounts = config.get("accounts", ["heron-mc", "likehupochuan"])
    resolved, missing = [], []
    for account in accounts:
        value = mapping.get(account, "")
        if "\n" in value or "\r" in value or parseaddr(value)[1] != value or "@" not in value:
            missing.append(account)
        else:
            resolved.append(value)
    if missing:
        raise ValueError("email mapping not configured for Gitee accounts: " + ", ".join(missing))
    if not resolved:
        raise ValueError("no notification recipients configured")
    return list(dict.fromkeys(resolved))


def send_email(config, subject, body):
    targets = recipients(config)
    host = config.get("host")
    sender = config.get("from")
    if not host or not sender:
        raise ValueError("SMTP host/from not configured")
    if "\r" in sender or "\n" in sender or "@" not in sender:
        raise ValueError("invalid SMTP from address")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(targets)
    message.set_content(body)
    use_ssl = bool(config.get("ssl", False))
    oauth = config.get('oauth2')
    validate_authentication(config)
    access_token = oauth_access_token(oauth) if oauth is not None else None
    port = int(config.get("port", 465 if use_ssl else 587))
    client_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    kwargs = {"timeout": 30}
    if use_ssl:
        kwargs["context"] = ssl.create_default_context()
    try:
        with client_type(host, port, **kwargs) as client:
            if not use_ssl and config.get("starttls", True):
                client.starttls(context=ssl.create_default_context())
            username = os.environ.get(config.get("username_env", "LOCAL_CI_SMTP_USERNAME"), "") or sender
            if access_token is not None:
                client.ehlo()
                response = 'user=' + username + '\x01auth=Bearer ' + access_token + '\x01\x01'
                client.auth('XOAUTH2', lambda challenge=None: response if challenge is None else '')
            else:
                username = os.environ.get(config.get("username_env", "LOCAL_CI_SMTP_USERNAME"), "")
                password = os.environ.get(config.get("password_env", "LOCAL_CI_SMTP_PASSWORD"), "")
                if username:
                    if not password:
                        raise ValueError("SMTP password environment variable is missing")
                    client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        if oauth is not None:
            raise ValueError('OAuth2 SMTP authentication or delivery failed') from None
        raise


class Notifier:
    def __init__(self, state_dir, config, send=send_email):
        self.root = Path(state_dir)
        self.config = config
        self.send = send

    def update(self, worker_id, issues, dry_run=False):
        """Notify only changed incidents/recovery; failed deliveries retry later."""
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id is required")
        key = hashlib.sha256(worker_id.encode()).hexdigest()[:24]
        # Drop fluctuating metrics from fingerprint to avoid mail each heartbeat.
        stable = sorted({json.dumps([item["code"], item.get("profile_id", ""), item.get("service", ""), item.get("path", "")]) for item in issues})
        signature = hashlib.sha256(json.dumps(stable).encode()).hexdigest()
        with file_lock(self.root / f"notify-{key}.lock"):
            path = self.root / f"notify-{key}.json"
            state = read_json(path, {})
            if state.get("signature") == signature:
                return {"status": "unchanged"}
            if not issues and not state.get("signature"):
                atomic_json(path, {"signature": signature, "updated_at": time.time()})
                return {"status": "healthy"}
            subject = f"[Local CI] {worker_id}: {'异常' if issues else '已恢复'}"
            body = "\n".join(f"- {item['code']}: {item.get('message', '')}" for item in issues) or "此前报告的 Local CI 故障已恢复。"
            if dry_run:
                return {"status": "dry_run", "subject": subject, "body": body}
            try:
                self.send(self.config, subject, body)
            except (ValueError, OSError, smtplib.SMTPException) as exc:
                # Do not persist exception text: servers can include credentials.
                atomic_json(path.with_suffix(".pending.json"), {"updated_at": time.time(), "error_type": type(exc).__name__, "issues": stable})
                return {"status": "pending", "error_type": type(exc).__name__}
            atomic_json(path, {"signature": signature, "updated_at": time.time()})
            path.with_suffix(".pending.json").unlink(missing_ok=True)
            return {"status": "sent"}
