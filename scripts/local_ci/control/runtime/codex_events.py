"""Read only Codex protocol errors, never command output or model messages."""
from __future__ import annotations

import json
import re
from pathlib import Path


SESSION = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
RETRY_CODES = {'server_is_overloaded', 'model_overloaded', 'model_at_capacity',
               'rate_limit_exceeded', 'rate_limit_error', 'slow_down'}
DENIED = re.compile(r'quota|credit|balance|billing|spend.?limit|usage.?limit|budget|'
                    r'authenticat|unauthori[sz]ed|permission|forbidden|api.?key|'
                    r'access.?denied|not.?authorized|insufficient.?funds', re.I)
TRANSIENT = re.compile(r'(?:selected |requested )?model (?:is )?(?:currently )?at capacity|'
                       r'(?:model|server|service) (?:is )?(?:temporarily |currently )?overloaded|'
                       r'rate[ -]limit(?: (?:reached|exceeded)|ed)|too many requests', re.I)


def read_events(path):
    """Return the session and terminal failure from one CLI invocation.

    A completed turn clears earlier reconnect messages. Billing/auth errors in
    the failing turn override rate-limit wording (both can have HTTP 429).
    """
    session_id, errors = None, []
    with Path(path).open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'thread.started':
                value = event.get('thread_id')
                if isinstance(value, str) and SESSION.fullmatch(value):
                    session_id = value
            elif kind in ('turn.started', 'turn.completed'):
                errors.clear()
            elif kind in ('error', 'turn.failed'):
                payload = event.get('error', event)
                if not isinstance(payload, dict):
                    payload = {'message': payload if isinstance(payload, str) else ''}
                error = {key: payload[key][:2000] for key in ('message', 'code', 'type')
                         if isinstance(payload.get(key), str)}
                # Do not mistake the envelope's type='error' for an API code.
                if payload is event:
                    error.pop('type', None)
                error['event'] = kind
                errors.append(error)
    if not errors:
        return session_id, None
    denied = any(DENIED.search(' '.join(e.get(k, '') for k in ('message', 'code', 'type')))
                 for e in errors)
    terminal = errors[-1]
    matching = [terminal] + [e for e in errors[:-1] if terminal.get('message') and
                             e.get('message') == terminal['message']]
    retryable = not denied and any(
        e.get('code') in RETRY_CODES or e.get('type') in RETRY_CODES or
        TRANSIENT.search(e.get('message', '')) for e in matching)
    return session_id, {**terminal, 'retryable': bool(retryable)}
