"""Git transport with immutable run artifacts and retryable publication."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .common import digest, read_json, safe_id, utcnow, write_json
try:
    from ..maintenance.transport import git_environment, validate_relay_url
except ImportError:
    from maintenance.transport import git_environment, validate_relay_url


def evidence_path(root, relative):
    """Resolve only canonical relative paths, rejecting every symlink component."""
    root = Path(root)
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('artifact path must be a canonical relative path')
    pure = PurePosixPath(relative)
    if pure.is_absolute() or '..' in pure.parts or pure.as_posix() != relative:
        raise ValueError('artifact path escapes the published run')
    target = root.joinpath(*pure.parts)
    if root.is_symlink() or any(root.joinpath(*pure.parts[:i]).is_symlink() for i in range(1, len(pure.parts) + 1)):
        raise ValueError('artifact symlink paths are not publishable')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('artifact path escapes the published run')
    return target


class Relay:
    def __init__(self, config, state_dir):
        self.config = config
        self.url = config['url']
        validate_relay_url(self.url)
        self.branch = config.get('results_branch', 'local-ci-results')
        self.root = Path(state_dir) / 'relay'
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'no-hooks').mkdir(exist_ok=True)
        self.mirror = self.root / 'objects.git'
        if not self.mirror.exists():
            self._git(self.root, 'init', '--bare', '--template=', str(self.mirror))
            self._git(self.mirror, 'remote', 'add', 'origin', self.url)
        self._git(self.mirror, 'config', '--local', 'core.hooksPath', str(self.root / 'no-hooks'))
        if self._git(self.mirror, 'remote', 'get-url', 'origin') != self.url:
            raise ValueError('relay URL changed for existing state')
        self._git(self.root, 'check-ref-format', '--branch', self.branch)

    def _git(self, repo, *args, check=True, binary=False):
        options = {'capture_output': True, 'timeout': int(self.config.get('git_timeout_seconds', 60)),
                   'env': git_environment(self.config)}
        if not binary:
            options.update(text=True, encoding='utf-8', errors='replace')
        # Keep hook policy repository-local. A command-line -c would propagate
        # through GIT_CONFIG_PARAMETERS into a local git-receive-pack server and
        # could suppress that server's pre-receive protection.
        result = subprocess.run(['git', '-C', str(repo), *map(str, args)], **options)
        if check and result.returncode:
            raise RuntimeError(f'git {args[0]} failed (exit {result.returncode})')
        if not check:
            return result
        return result.stdout if binary else result.stdout.strip()

    def fetch(self):
        self._git(self.mirror, 'fetch', '--prune', '--no-tags', 'origin', '+refs/heads/*:refs/heads/*')

    def refs(self):
        return dict(line.split(' ', 1) for line in self._git(self.mirror, 'for-each-ref',
                    '--format=%(refname:short) %(objectname)', 'refs/heads/').splitlines())

    def document(self, ref, name):
        raw = self._git(self.mirror, 'show', ref + ':' + name, check=False)
        return json.loads(raw.stdout) if raw.returncode == 0 else None

    def task(self, task_ref):
        return self.document(task_ref.replace('ci/', 'ci/meta/', 1), 'task-metadata.json')

    def current(self, task):
        self.fetch()
        refs = self.refs()
        if refs.get(task['task_ref']) != task['tested_sha']:
            return False
        live = self.task(task['task_ref'])
        identity_keys = ('task_id', 'repository', 'task_ref', 'tested_sha', 'head_sha',
                         'base_sha', 'target_branch', 'worker_revision_sha', 'approval', 'preflight')
        if not live or any(live.get(key) != task.get(key) for key in identity_keys):
            return False
        if task['event_kind'] == 'pull_request':
            cancellation = self.document(task['task_ref'].replace('ci/', 'ci/cancel/', 1), 'cancellation.json')
            if cancellation and cancellation.get('repository') == task['repository']:
                effective = (cancellation.get('target_branch') == task['target_branch'] and
                    datetime.fromisoformat(cancellation['cancelled_at'].replace('Z', '+00:00')) >=
                    datetime.fromisoformat(task['captured_at'].replace('Z', '+00:00')))
                if effective and (cancellation.get('cancel_all') or cancellation.get('head_sha') == task['head_sha']):
                    return False
        return True

    def checkout(self, sha, destination):
        """Materialize the frozen tree without hooks, config, or automatic submodules."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        data = self._git(self.mirror, 'archive', sha, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(destination, filter='data')
        snapshot = {str(path.relative_to(destination)).replace('\\', '/'): digest(path)
                for path in destination.rglob('*') if path.is_file() and not path.is_symlink()}
        snapshot.update({path.relative_to(destination).as_posix(): 'symlink:' + os.readlink(path)
                         for path in destination.rglob('*') if path.is_symlink()})
        for line in self._git(self.mirror, 'ls-tree', '-r', sha).splitlines():
            if line.startswith('160000 commit '):
                object_info, name = line.split('\t', 1)
                snapshot[name] = 'gitlink:' + object_info.split()[2]
        self._git(self.root, 'init', '--template=', str(destination))
        self._git(destination, 'config', 'core.hooksPath', '/dev/null')
        self._git(destination, 'fetch', '--no-tags', str(self.mirror.resolve()), sha)
        self._git(destination, 'reset', '--mixed', sha)
        return snapshot

    def changed_paths(self, task):
        return self._git(self.mirror, 'diff', '--no-renames', '--name-only', task['base_sha'], task['tested_sha']).splitlines()

    def publish(self, result_dir, result):
        task_id, run_id = safe_id(result['task_id']), safe_id(result['run_id'])
        worktree = self.root / 'publication'
        if not worktree.exists():
            self._git(self.root, 'init', '--template=', str(worktree))
            self._git(worktree, 'remote', 'add', 'origin', self.url)
            self._git(worktree, 'config', 'user.name', 'Local CI')
            self._git(worktree, 'config', 'user.email', 'local-ci@localhost')
        self._git(worktree, 'config', '--local', 'core.hooksPath', str(self.root / 'no-hooks'))
        # This checkout is private to the publisher; user working trees are never reset.
        fetched = self._git(worktree, 'fetch', 'origin', self.branch, check=False)
        if fetched.returncode == 0:
            self._git(worktree, 'checkout', '-B', 'publication', 'FETCH_HEAD')
        elif self._git(worktree, 'rev-parse', '--verify', 'HEAD', check=False).returncode:
            self._git(worktree, 'checkout', '--orphan', 'publication')
        relative = f'runs/{task_id}/{run_id}'
        target = evidence_path(worktree, relative)
        if target.exists():
            old_result = target / 'result.json'
            if old_result.exists() and digest(old_result) != digest(Path(result_dir) / 'result.json'):
                raise ValueError('immutable run already exists with different content')
        target.mkdir(parents=True, exist_ok=True)
        files = []
        # Only explicit result files and bounded task artifacts leave the host.
        allowed = ('result.json', 'report.md', 'evidence.json', 'agent-review.json', 'checks.json', 'artifact-manifest.json')
        candidates = [Path(result_dir) / name for name in allowed]
        candidates += list((Path(result_dir) / 'logs').glob('command-*.log'))
        artifact_manifest = Path(result_dir) / 'artifact-manifest.json'
        if artifact_manifest.exists():
            artifact_manifest = evidence_path(result_dir, 'artifact-manifest.json')
            manifest = read_json(artifact_manifest)
            if manifest.get('schema') != 'triton-anchor-local-ci-artifacts' or not isinstance(manifest.get('files'), list):
                raise ValueError('artifact manifest schema/files are invalid')
            seen = set()
            for entry in manifest['files']:
                name = entry.get('path')
                if not isinstance(name, str) or not name.startswith('artifacts/') or name in seen:
                    raise ValueError('artifact manifest must list unique artifacts/ paths')
                seen.add(name)
                artifact = evidence_path(result_dir, name)
                if not isinstance(entry.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
                    raise ValueError('artifact manifest digest is invalid')
                if not artifact.is_file() or artifact.stat().st_size != entry.get('size') or digest(artifact) != entry['sha256']:
                    raise ValueError('frozen artifact is missing or its digest/size changed')
                candidates.append(artifact)
        for receipt in result.get('evidence', []):
            log = evidence_path(result_dir, receipt['log_path'])
            if not log.is_file() or log.is_symlink() or digest(log) != receipt['log_sha256']:
                raise ValueError('host receipt log is missing or its digest changed')
        candidate_names = set()
        for source in candidates:
            if source.exists() or source.is_symlink():
                name = source.relative_to(result_dir).as_posix()
                evidence_path(result_dir, name)
                if not source.is_file():
                    raise ValueError('published evidence must be a regular file')
                if source.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError('evidence exceeds 20 MiB publication limit; retained locally: ' + name)
                candidate_names.add(name)
        previous_manifest = target / 'publish-manifest.json'
        if previous_manifest.exists():
            old_files = read_json(evidence_path(target, 'publish-manifest.json'))['files']
            if {entry['path'] for entry in old_files} != candidate_names:
                raise ValueError('immutable published artifact set differs from retry content')
            for entry in old_files:
                candidate = evidence_path(result_dir, entry['path'])
                if not candidate.is_file() or candidate.is_symlink() or digest(candidate) != entry['sha256']:
                    raise ValueError('immutable published artifact differs from retry content')
        for source in candidates:
            if not source.is_file() or source.is_symlink():
                continue
            name = source.relative_to(result_dir).as_posix()
            dest = evidence_path(target, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            files.append({'path': name, 'sha256': digest(dest), 'size': dest.stat().st_size})
        write_json(evidence_path(target, 'publish-manifest.json'), {'schema': 'triton-anchor-local-ci-publication',
                   'task_id': task_id, 'run_id': run_id, 'files': files})
        write_json(evidence_path(worktree, f'tasks/{task_id}/latest.json'), {'task_id': task_id, 'run_id': run_id,
                   'result_path': relative + '/result.json', 'result_sha256': digest(target / 'result.json'),
                   'manifest_path': relative + '/publish-manifest.json'})
        self._git(worktree, 'add', '--', relative, f'tasks/{task_id}/latest.json')
        if self._git(worktree, 'diff', '--cached', '--quiet', check=False).returncode:
            self._git(worktree, 'commit', '-m', f'Local CI result {task_id} {run_id}')
        for attempt in range(3):
            pushed = self._git(worktree, 'push', 'origin', f'HEAD:refs/heads/{self.branch}', check=False)
            if pushed.returncode == 0:
                return relative
            self._git(worktree, 'fetch', 'origin', self.branch)
            rebased = self._git(worktree, 'rebase', 'FETCH_HEAD', check=False)
            if rebased.returncode:
                self._git(worktree, 'rebase', '--abort', check=False)
                raise RuntimeError('result publication conflict; retained for retry')
        raise RuntimeError('result publication failed; retained for retry')
