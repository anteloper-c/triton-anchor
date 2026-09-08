#!/usr/bin/env python3
"""Trusted Linux worker launcher with bounded process cleanup.

Use the root-owned /usr/bin/python3 -I interpreter. The host alone invokes
clean-users as uid 0 after a task; it takes no PR-controlled user identifiers.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time


def process_info(pid):
    """Read Linux identity; start_ticks prevents signalling a reused PID."""
    try:
        root = Path('/proc') / str(pid)
        content = (root / 'stat').read_text()
        fields = content[content.rfind(')') + 2:].split()
        uid_line = next(line for line in (root / 'status').read_text().splitlines() if line.startswith('Uid:'))
        return {'pid': int(pid), 'state': fields[0], 'pgrp': int(fields[2]),
                'start_ticks': fields[19], 'uid': int(uid_line.split()[2])}
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, StopIteration):
        return None


def processes():
    for path in Path('/proc').iterdir():
        if path.name.isdecimal():
            info = process_info(int(path.name))
            if info is not None and info['state'] != 'Z':
                yield info


def process_directory():
    """Each execution UID owns a private directory, including before Popen."""
    root = Path('/tmp') / f'anchor-ci-processes-{os.geteuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError('unsafe process directory ownership/type')
    root.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix='.writable-', dir=root)
    os.close(fd)
    Path(temporary).unlink()
    return root


def atomic_pidfile(path, value):
    fd, name = tempfile.mkstemp(prefix='.pid-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def group_members(pgid):
    return [p for p in processes() if p['pgrp'] == pgid and p['uid'] == os.geteuid()]


def stop_group(info, grace=3.0):
    pid = int(info['pid'])
    if pid <= 1 or int(info['uid']) != os.geteuid():
        raise RuntimeError('unsafe process group identity')
    leader = process_info(pid)
    if leader and (leader['uid'] != os.geteuid() or leader['start_ticks'] != info['start_ticks'] or leader['pgrp'] != pid):
        raise RuntimeError('process identity changed; refusing a stale PID')
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_members(pid):
            return
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not group_members(pid):
                return
            time.sleep(0.05)
    if group_members(pid):
        raise RuntimeError('process group remains alive after SIGKILL')


def clean_users():
    """Root-only final sweep also catches descendants that used setsid()."""
    if os.geteuid() != 0:
        raise PermissionError('clean-users requires the trusted host to exec as root')
    users = {1000, 1001}
    for sig, duration in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
        deadline = time.monotonic() + duration
        while True:
            members = [p for p in processes() if p['uid'] in users]
            if not members:
                # A SIGKILLed launcher cannot unlink its own record. Only remove
                # direct pidfile entries after proving these UIDs have no live
                # processes; never follow a task-created directory symlink.
                for uid in users:
                    folder = Path('/tmp') / f'anchor-ci-processes-{uid}'
                    if not folder.exists():
                        continue
                    metadata = folder.lstat()
                    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid:
                        raise RuntimeError('unsafe process directory during recovery')
                    for item in folder.glob('*.json'):
                        item.unlink()
                print(json.dumps({'status': 'clean', 'users': sorted(users)}))
                return 0
            for old in members:
                current = process_info(old['pid'])
                if current and current['uid'] in users and current['start_ticks'] == old['start_ticks']:
                    try:
                        os.kill(current['pid'], sig)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise RuntimeError('task user processes remain alive; retain worker lease for recovery')


def validate_spec(spec):
    if not isinstance(spec, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', str(spec.get('id', ''))):
        raise ValueError('invalid process identity')
    argv = spec.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv):
        raise ValueError('argv must be a nonempty string array')
    if not isinstance(spec.get('cwd'), str) or not Path(spec['cwd']).is_absolute():
        raise ValueError('cwd must be an absolute path')
    env = spec.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '=' in k or '\x00' in k + v for k, v in env.items()):
        raise ValueError('invalid command environment')


def run(spec):
    validate_spec(spec)
    pidfile = process_directory() / (spec['id'] + '.json')
    if pidfile.exists():
        raise RuntimeError('process identity already registered; stop/recover it before retry')
    env = os.environ.copy()
    env.update(spec.get('env', {}))
    proc = None
    identity = None

    def interrupted(signum, frame):
        raise InterruptedError(f'launcher received signal {signum}')

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        proc = subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=env, start_new_session=True)
        identity = process_info(proc.pid)
        if identity is None:
            raise RuntimeError('child process identity unavailable')
        atomic_pidfile(pidfile, identity)
        return proc.wait()
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if proc is not None:
                if identity is not None:
                    stop_group(identity)
                elif proc.poll() is None:
                    proc.kill()
                try:
                    proc.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            pidfile.unlink(missing_ok=True)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['run', 'stop', 'clean-users'])
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args(argv)
    if args.operation == 'clean-users':
        if args.payload is not None:
            parser.error('clean-users accepts no task-controlled arguments')
        return clean_users()
    if args.payload is None:
        parser.error('run/stop require an encoded trusted process specification')
    spec = json.loads(base64.urlsafe_b64decode(args.payload))
    validate_spec(spec)
    if args.operation == 'run':
        return run(spec)
    pidfile = process_directory() / (spec['id'] + '.json')
    if pidfile.exists():
        if pidfile.is_symlink():
            raise RuntimeError('unsafe pidfile symlink')
        stop_group(json.loads(pidfile.read_text(encoding='utf-8')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
