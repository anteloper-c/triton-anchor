"""SMTP incident/recovery notifications with durable deduplication and retries."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import parseaddr
import hashlib
import json
import os
from pathlib import Path
import smtplib
import ssl
import time

from .workers import atomic_json, file_lock, read_json


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
    port = int(config.get("port", 465 if use_ssl else 587))
    client_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    kwargs = {"timeout": 30}
    if use_ssl:
        kwargs["context"] = ssl.create_default_context()
    with client_type(host, port, **kwargs) as client:
        if not use_ssl and config.get("starttls", True):
            client.starttls(context=ssl.create_default_context())
        username = os.environ.get(config.get("username_env", "LOCAL_CI_SMTP_USERNAME"), "")
        password = os.environ.get(config.get("password_env", "LOCAL_CI_SMTP_PASSWORD"), "")
        if username:
            if not password:
                raise ValueError("SMTP password environment variable is missing")
            client.login(username, password)
        client.send_message(message)


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
