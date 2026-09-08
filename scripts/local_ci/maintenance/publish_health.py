"""Publish health in a private checkout; no dependency on the task publisher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from .transport import git_environment, validate_relay_url
from .workers import atomic_json, file_lock, read_json


class PublicationError(RuntimeError):
    pass


class HealthPublisher:
    def __init__(self, config, run=subprocess.run):
        self.config = config
        self.relay = config['relay']
        self.url = self.relay['url']
        self.branch = self.relay.get('results_branch', 'local-ci-results')
        self.worker_id = config['worker_id']
        self.root = Path(config['state_dir']) / 'health-publication'
        self.checkout = self.root / 'repo'
        self.run = run
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', self.worker_id):
            raise PublicationError('invalid worker_id')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', self.branch):
            raise PublicationError('invalid results branch')
        try:
            validate_relay_url(self.url)
        except ValueError as exc:
            raise PublicationError(str(exc)) from exc

    def _environment(self):
        return git_environment(self.relay)

    def _git(self, *args, check=True):
        result = self.run(['git', '-C', str(self.checkout), *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=int(self.relay.get('git_timeout_seconds', 60)), env=self._environment())
        if check and result.returncode:
            # Do not copy server errors or credential-bearing process environment
            # into health snapshots or SMTP messages.
            raise PublicationError(f'health git {args[0]} failed (exit {result.returncode})')
        return result

    def _prepare(self):
        owner = {'relay_url': self.url, 'results_branch': self.branch, 'worker_id': self.worker_id}
        marker = self.root / 'owner.json'
        if marker.exists():
            if read_json(marker) != owner:
                raise PublicationError('existing health publication workspace has a different owner/relay')
        elif self.checkout.exists():
            raise PublicationError('refusing to adopt an unmanaged publication checkout')
        self.checkout.mkdir(parents=True, exist_ok=True)
        atomic_json(marker, owner)
        (self.root / 'no-hooks').mkdir(exist_ok=True)
        if not (self.checkout / '.git').is_dir():
            self._git('init', '--template=')
            self._git('remote', 'add', 'origin', self.url)
            self._git('config', 'user.name', 'Local CI Health')
            self._git('config', 'user.email', 'local-ci-health@localhost')
        self._git('config', '--local', 'core.hooksPath', str(self.root / 'no-hooks'))
        if self._git('remote', 'get-url', 'origin').stdout.strip() != self.url:
            raise PublicationError('health publication remote differs from trusted configuration')
        self._git('check-ref-format', '--branch', self.branch)
        fetched = self._git('fetch', '--no-tags', 'origin', self.branch, check=False)
        if fetched.returncode == 0:
            self._git('checkout', '-B', 'health-publication', 'FETCH_HEAD')
            return
        refs = self._git('ls-remote', '--heads', 'origin', 'refs/heads/' + self.branch)
        if refs.stdout.strip():
            raise PublicationError('results branch exists but could not be fetched')
        if self._git('rev-parse', '--verify', 'HEAD', check=False).returncode:
            self._git('checkout', '--orphan', 'health-publication')

    def _snapshot(self):
        source = Path(self.config['state_dir']) / 'health' / 'latest.json'
        if source.is_symlink() or source.stat().st_size > 2 * 1024 * 1024:
            raise PublicationError('health snapshot must be a bounded regular file')
        snapshot = read_json(source)
        if not isinstance(snapshot, dict) or snapshot.get('schema') != 'triton-anchor-local-ci-worker-health' or snapshot.get('worker_id') != self.worker_id:
            raise PublicationError('health snapshot schema/identity differs from trusted configuration')
        if not isinstance(snapshot.get('issues'), list) or snapshot.get('state') not in {'healthy', 'degraded'}:
            raise PublicationError('health snapshot state/issues are invalid')
        return snapshot

    def publish(self):
        """Publish one snapshot; local bare Git and HTTPS share the same workflow."""
        with file_lock(self.root / 'publication.lock'):
            snapshot = self._snapshot()
            self._prepare()
            relative = 'health/' + self.worker_id + '.json'
            target = self.checkout / relative
            if (self.checkout / 'health').is_symlink() or target.is_symlink() or not target.resolve().is_relative_to(self.checkout.resolve()):
                raise PublicationError('unsafe published health path')
            atomic_json(target, snapshot)
            self._git('add', '--', relative)
            changed = self._git('diff', '--cached', '--quiet', check=False).returncode
            if changed:
                self._git('commit', '-m', 'Local CI health ' + self.worker_id)
            # A previous failed publication may already be committed locally even
            # when today's snapshot is unchanged, so always attempt a bounded push.
            for attempt in range(3):
                pushed = self._git('push', 'origin', 'HEAD:refs/heads/' + self.branch, check=False)
                if pushed.returncode == 0:
                    result = {'status': 'published', 'worker_id': self.worker_id, 'path': relative, 'published_at': time.time()}
                    atomic_json(self.root / 'latest.json', result)
                    return result
                fetched = self._git('fetch', '--no-tags', 'origin', self.branch, check=False)
                if fetched.returncode:
                    continue
                rebased = self._git('rebase', 'FETCH_HEAD', check=False)
                if rebased.returncode:
                    self._git('rebase', '--abort', check=False)
                    raise PublicationError('health publication conflict; local snapshot retained for retry')
            raise PublicationError('health publication failed; local snapshot retained for retry')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    try:
        result = HealthPublisher(config).publish()
    except (PublicationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {'status': 'pending', 'error_type': type(exc).__name__, 'updated_at': time.time()}
        atomic_json(Path(config['state_dir']) / 'health-publication/latest.json', result)
        print(json.dumps(result))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
