"""Persistent queue: restart resumes publication before accepting another task."""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from maintenance.workers import WorkerManager, WorkerBusy, file_lock
from .common import read_json, utcnow, write_json
from .engine import Engine
from .policy import validate_task
from .relay import Relay


class Poller:
    def __init__(self, config):
        self.config = config
        self.state = Path(config['state_dir'])
        self.state.mkdir(parents=True, exist_ok=True)
        self.relay = Relay(config['relay'], self.state)
        self.manager = WorkerManager(self.state, docker=config.get('docker', 'docker'))
        self.engine = Engine(config, self.relay, self.manager)
        self.profiles = {p['id']: p for p in config['profiles']}

    def retry_publications(self):
        pending = False
        for record in sorted((self.state / 'runs').glob('*/*/execution.json')):
            try:
                execution = read_json(record)
                if execution['phase'] != 'publish_pending':
                    continue
                result = read_json(record.parent / 'result.json')
                self.engine.heartbeat('publishing', result['task_id'])
                self.relay.publish(record.parent, result)
                execution['phase'] = 'published'
                execution['published_at'] = utcnow()
                write_json(record, execution)
                write_json(self.state / 'completed' / (result['task_id'] + '.json'),
                           {'run_id': result['run_id'], 'conclusion': result['conclusion'],
                            'published_at': execution['published_at']})
                self.engine.heartbeat('completed', result['task_id'])
            except Exception as exc:
                pending = True
                self.engine.heartbeat('publish_pending', record.parent.parent.name, str(exc))
                write_json(record.with_name('publication-error.json'), {'error': str(exc), 'at': utcnow()})
                try:
                    execution = read_json(record)
                    execution['publish_attempts'] = execution.get('publish_attempts', 0) + 1
                    execution['publish_error'] = str(exc)
                    write_json(record, execution)
                    if hasattr(self.engine, 'recover_publication'):
                        try:
                            self.engine.recover_publication(record, str(exc))
                        except Exception as recovery_exc:
                            write_json(record.with_name('publication-recovery-error.json'),
                                       {'error': str(recovery_exc), 'at': utcnow()})
                except (ValueError, OSError):
                    pass
        return pending

    def recover_interrupted(self):
        """A restarted poller owns the global OS lock before recovering worker leases."""
        for record in sorted((self.state / 'runs').glob('*/*/execution.json')):
            try:
                execution = read_json(record)
                if execution.get('phase') not in ('preparing', 'running'):
                    continue
                task = read_json(record.parent / 'task.json')
                profile = self.profiles[execution['profile_id']]
                status = self.manager.inspect(profile)
                lease = status.get('lease')
                if lease and lease['task_id'] != task['task_id']:
                    raise RuntimeError('another task holds the worker; recovery deferred')
                if status['running']:
                    self.engine.docker_run(profile, '/usr/bin/python3', '-I',
                          '/opt/anchor-ci/runtime/container_process.py', 'clean-users')
                self.manager.release(profile, task['task_id'])
                self.engine.run(task, profile, resume_record=record)
            except Exception as exc:
                self.engine.heartbeat('error', record.parent.parent.name, 'recovery: ' + str(exc))
                write_json(record.with_name('recovery-error.json'), {'error': str(exc), 'at': utcnow()})

    def once(self):
        self.recover_interrupted()
        pending = self.retry_publications()
        self.relay.fetch()
        refs = self.relay.refs()
        outcomes = []
        for ref, sha in sorted(refs.items()):
            if not (ref.startswith('ci/pr-') or ref.startswith('ci/push/') or ref.startswith('ci/full/')):
                continue
            task = self.relay.task(ref)
            if not task or task.get('tested_sha') != sha:
                continue
            try:
                validate_task(task, self.config.get('repository', 'anteloper-c/triton-anchor'))
                if (self.state / 'completed' / (task['task_id'] + '.json')).exists():
                    continue
                if list((self.state / 'runs' / task['task_id']).glob('*/execution.json')):
                    # A crash never silently duplicates a running build. Recovery is explicit.
                    continue
                profile_id = self.config['branch_profiles'][task['target_branch']]
                profile = self.profiles[profile_id]
                output, result = self.engine.run(task, profile)
                outcomes.append(result)
                self.retry_publications()
            except WorkerBusy:
                continue
            except Exception as exc:
                self.engine.heartbeat('error', task.get('task_id'), str(exc))
                name = hashlib.sha256(ref.encode()).hexdigest()
                write_json(self.state / 'rejected' / (name + '.json'),
                           {'task_ref': ref, 'error': str(exc), 'at': utcnow()})
        pending = self.retry_publications()
        if not pending:
            self.engine.heartbeat('idle')
        return outcomes

    def serve(self, once=False):
        with file_lock(self.state / 'poller.lock'):
            while True:
                try:
                    self.once()
                except Exception as exc:
                    self.engine.heartbeat('error', error=str(exc))
                    if once:
                        raise
                if once:
                    return
                time.sleep(self.config.get('poll_interval', 30))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    Poller(read_json(args.config)).serve(args.once)
    return 0
