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
from .policy import TOOLS, identity, validate_task
from .report import markdown
from .relay import Relay
from .result_paths import iter_run_files, run_relative, task_run_files, validate_result_path


class Poller:
    def __init__(self, config):
        self.config = config
        self.state = Path(config['state_dir'])
        self.state.mkdir(parents=True, exist_ok=True)
        self.relay = Relay(config['relay'], self.state)
        self.manager = WorkerManager(self.state, docker=config.get('docker', 'docker'))
        self.engine = Engine(config, self.relay, self.manager)
        self.profiles = {p['id']: p for p in config['profiles']}

    def reject_task(self, task, reason, *, cancelled=False):
        """Persist an admission failure through the normal immutable result publisher."""
        output = self.state / run_relative(task, 'admission')
        output.mkdir(parents=True, exist_ok=True)
        now = utcnow()
        conclusion = 'cancelled' if cancelled else 'error'
        checks = [{'id': tool, 'required': False, 'status': 'skipped',
                   'reason': 'task was rejected before tool execution', 'evidence': []} for tool in TOOLS]
        checks.extend([{'id': 'architecture_review', 'required': True, 'status': 'error',
                        'reason': 'task was rejected before AI review', 'evidence': []},
                       {'id': 'pr_information', 'required': task['event_kind'] == 'pull_request',
                        'status': 'error' if task['event_kind'] == 'pull_request' else 'not_applicable',
                        'reason': 'task was rejected before AI review' if task['event_kind'] == 'pull_request' else 'not a PR event',
                        'evidence': []}])
        result = {'schema': 'triton-anchor-local-ci-result', **identity(task), 'run_id': 'admission',
                  'conclusion': conclusion, 'checks': checks, 'blocking_reasons': [reason],
                  'ai_review': {}, 'evidence': [], 'performance': [], 'source_unchanged': False,
                  'policy': {'required': [check['id'] for check in checks if check['required']],
                             'admission': 'rejected', 'reason': reason},
                  'control_identity': {'verified': False},
                  'validation_scope': 'local_acceptance' if self.config.get('local_acceptance') else 'production',
                  'started_at': now, 'completed_at': now}
        write_json(output / 'task.json', task)
        write_json(output / 'result.json', result)
        (output / 'report.md').write_text(markdown(result), encoding='utf-8')
        write_json(self.state / 'rejected' / (task['task_id'] + '.json'),
                   {**identity(task), 'conclusion': conclusion, 'reason': reason, 'at': now})
        # Journal last: an interrupted write is rebuilt before any publication.
        write_json(output / 'execution.json', {'phase': 'publish_pending', 'run_id': 'admission',
                   'profile_id': None, 'started_at': now})
        return result

    def retry_publications(self):
        pending = False
        for record in iter_run_files(self.state, 'execution.json'):
            try:
                execution = read_json(record)
                if execution['phase'] != 'publish_pending':
                    continue
                result = read_json(record.parent / 'result.json')
                validate_result_path(record.relative_to(self.state).as_posix(), result,
                                     result['run_id'], 'execution.json')
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
        if pending:
            # A later successful run must not clear another run's publication
            # fault from the shared queue heartbeat used by the watchdog.
            self.engine.heartbeat('publish_pending')
        return pending

    def recover_interrupted(self):
        """A restarted poller owns the global OS lock before recovering worker leases."""
        for record in iter_run_files(self.state, 'execution.json'):
            try:
                execution = read_json(record)
                if execution.get('phase') not in ('preparing', 'running'):
                    continue
                task = read_json(record.parent / 'task.json')
                validate_result_path(record.relative_to(self.state).as_posix(), task,
                                     execution['run_id'], 'execution.json')
                profile = self.profiles[execution['profile_id']]
                status = self.manager.inspect(profile)
                lease = status.get('lease')
                if lease and lease['task_id'] != task['task_id']:
                    raise RuntimeError('another task holds the worker; recovery deferred')
                if status['running']:
                    self.engine.docker_run(profile, '/usr/bin/python3', '-I',
                          '/opt/anchor-ci/control/runtime/container_process.py', 'clean-users')
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
                if task_run_files(self.state, task):
                    # A crash never silently duplicates a running build. Recovery is explicit.
                    continue
                if not self.relay.current(task):
                    outcomes.append(self.reject_task(task, 'Task is no longer current or has been cancelled', cancelled=True))
                    continue
                profile_id = self.config['branch_profiles'].get(task['target_branch'])
                if profile_id not in self.profiles:
                    outcomes.append(self.reject_task(task,
                        'No trusted environment profile is configured for target branch: ' + task['target_branch']))
                    continue
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
