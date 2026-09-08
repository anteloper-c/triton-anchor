"""Small host-owned I/O and process primitives; no PR configuration is sourced."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('invalid task/run identifier')
    return value


def git(repo, *args, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], text=True,
                            encoding='utf-8', errors='replace', capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: {result.stderr[-1200:]}')
    return result.stdout.strip() if check else result


def execute(argv, log, *, cwd=None, env=None, timeout=900, cancelled=lambda: False,
            terminate=None):
    """Record bounded output; timeout/cancellation are distinct from a failed test.

    Docker callers supply terminate to kill the in-container process group: killing
    the docker client alone does not stop compilation inside a persistent worker.
    """
    started = time.monotonic()
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with Path(log).open('wb') as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        reason = None
        stopped_inside = False
        try:
            while process.poll() is None:
                if cancelled():
                    reason = 'cancelled'
                elif time.monotonic() - started >= timeout:
                    reason = 'timeout'
                if reason:
                    if terminate:
                        stopped_inside = True
                        terminate()
                    break
                time.sleep(.2)
        finally:
            if process.poll() is None:
                try:
                    # Includes exceptions from cancellation checks and monitors.
                    if terminate and not stopped_inside:
                        terminate()
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    return {'returncode': process.returncode, 'termination': reason,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'log_sha256': digest(log)}
