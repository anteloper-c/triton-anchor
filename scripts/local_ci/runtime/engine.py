"""Lease one persistent worker and run Codex as the build/test coordinator."""
from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import re
import subprocess
import threading
import time
from pathlib import Path
from maintenance.workers import WorkerBusy

from .broker import Broker
from .common import digest, execute, git, read_json, utcnow, write_json
from .policy import minimum_checks, validate_task
from .report import build_result, markdown


class Engine:
    def __init__(self, config, relay, manager):
        self.config, self.relay, self.manager = config, relay, manager
        self.state = Path(config['state_dir'])
        self.workspace = Path(config['workspace_host'])
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.docker = config.get('docker', 'docker')

    def heartbeat(self, phase, task_id=None, error=None):
        record = {'heartbeat_at': time.time(), 'state': phase, 'task_id': task_id, 'error': error}
        write_json(self.state / 'health/poller.json', record)
        if task_id:
            write_json(self.state / 'health/task.json', {**record, 'phase': phase,
                       'started_at': getattr(self, 'task_started', time.time())})

    def docker_run(self, profile, *args, user='0'):
        result = subprocess.run([self.docker, 'exec', '--user', user,
                  profile['container']['name'], *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError('worker preparation failed: ' + result.stderr[-1000:])
        return result.stdout

    def prepare_agent_home(self, profile):
        settings = self.config['codex']
        agent_home = settings.get('home', '/home/agent/.codex')
        self.docker_run(profile, 'mkdir', '-p', agent_home)
        # Copy only explicitly selected authentication, never the desktop config/plugins.
        if settings.get('auth_file'):
            subprocess.run([self.docker, 'cp', settings['auth_file'],
                profile['container']['name'] + ':' + agent_home + '/auth.json'],
                check=True, capture_output=True, timeout=30)
        self.docker_run(profile, 'chown', '-R', profile.get('agent_user', '1001:1000'), agent_home)
        self.docker_run(profile, 'chmod', '700', agent_home)
        return agent_home

    def run(self, task, profile, resume_record=None):
        validate_task(task, self.config.get('repository', 'anteloper-c/triton-anchor'))
        configured_profile = profile
        profile = copy.deepcopy(profile)
        # Acquire before creating an execution journal: maintenance/busy means queued.
        try:
            from .control import verify_control, verify_container_control
            control_identity = verify_control(self.config, task)
            if not self.config.get('local_acceptance'):
                from .environment import resolve_profile
                profile = resolve_profile(self.config, self.relay, task,
                                          self.manager.effective_profile(configured_profile))
                if profile['llvm_selection']['rebuild_required']:
                    rebuilt = self.manager.rebuild(profile, force=True)
                    if rebuilt['status'] == 'waiting':
                        raise WorkerBusy('waiting for the trusted LLVM environment rebuild')
                    if rebuilt['status'] != 'ready':
                        raise RuntimeError('trusted LLVM environment rebuild failed: ' + str(rebuilt))
                self.manager.prepare(profile)
                self.manager.save_selection(configured_profile, profile)
            else:
                self.manager.ensure(profile)
            self.manager.acquire(profile, task['task_id'])
            try:
                control_identity['container'] = verify_container_control(self.config, profile, control_identity)
            except Exception:
                self.manager.release(profile, task['task_id'])
                raise
        except WorkerBusy:
            raise
        except Exception as exc:
            run_id = (read_json(resume_record)['run_id'] if resume_record else
                      time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + secrets.token_hex(3))
            output = self.state / 'runs' / task['task_id'] / run_id
            output.mkdir(parents=True, exist_ok=True)
            policy = minimum_checks([], profile, task['task_ref'].startswith('ci/full/'))
            broker = Broker(profile, {}, policy, output / 'preparation-error', lambda: False)
            result = build_result(task, run_id, policy, broker, started_at=utcnow(),
                source_unchanged=False, agent_exitcode=1, error='worker preparation failed: ' + str(exc))
            write_json(output / 'task.json', task)
            write_json(output / 'result.json', result)
            (output / 'report.md').write_text(markdown(result), encoding='utf-8')
            write_json(output / 'execution.json', {'profile_id': profile['id'], 'run_id': run_id,
                       'phase': 'publish_pending', 'started_at': result['started_at']})
            return output, result
        run_id = (read_json(resume_record)['run_id'] if resume_record else
                  time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + secrets.token_hex(3))
        output = self.state / 'runs' / task['task_id'] / run_id
        output.mkdir(parents=True, exist_ok=True)
        started_at = utcnow()
        write_json(output / 'task.json', task)
        write_json(output / 'execution.json', {'profile_id': profile['id'], 'run_id': run_id,
                   'started_at': started_at, 'phase': 'preparing'})
        self.task_started = time.time()
        preparation_error = None
        changed = []
        try:
            if not self.relay.current(task):
                raise ValueError('task is no longer current')
            changed = self.relay.changed_paths(task)
        except Exception as exc:
            preparation_error = str(exc)
        policy = minimum_checks(changed, profile, task['task_ref'].startswith('ci/full/'))
        host_task = self.workspace / 'tasks' / task['task_id'] / run_id
        host_task.mkdir(parents=True, exist_ok=True)
        source = host_task / 'source'
        tracked_hashes = {}
        try:
            if resume_record:
                tracked_hashes = read_json(output / 'source-manifest.json')
            else:
                tracked_hashes = self.relay.checkout(task['tested_sha'], source)
                write_json(output / 'source-manifest.json', tracked_hashes)
        except Exception as exc:
            preparation_error = str(exc)
        for directory in ('artifacts/custom', 'agent'):
            (host_task / directory).mkdir(parents=True, exist_ok=True)
        container_task = '/workspace/tasks/' + task['task_id'] + '/' + run_id
        context = {'task_id': task['task_id'], 'target_sha': task['tested_sha'],
                   'control_identity': control_identity,
                   'validation_scope': 'local_acceptance' if self.config.get('local_acceptance') else 'production',
                   'source_dir': container_task + '/source', 'source_host_dir': str(source),
                   'artifact_dir': container_task + '/artifacts',
                   'artifact_host_dir': str(host_task / 'artifacts'), 'base_sha': task['base_sha'],
                   'tools_dir': '/opt/anchor-ci/tools', 'triton_version': profile['triton_version'],
                   'python_bin': '/opt/anchor-ci/runtime/task_python',
                   'task_venv': container_task + '/venv',
                   'manual_full': policy['manual_full'], 'changed_files': changed,
                   'changed_paths': changed, 'profile': profile}
        cancelled = threading.Event()
        stopped = threading.Event()
        monitor_errors = []
        lease = True
        try:
            broker = Broker(profile, context, policy, output, cancelled.is_set, docker=self.docker,
                            max_commands=self.config['codex'].get('max_commands', 100),
                            max_seconds=self.config['codex'].get('timeout', 14400))
        except (ValueError, OSError, KeyError) as exc:
            preparation_error = 'saved execution evidence cannot be resumed: ' + str(exc)
            broker = Broker(profile, context, policy, output / 'recovery-error', cancelled.is_set)
        exitcode, failure = 1, None
        write_json(output / 'task.json', task)
        write_json(output / 'execution.json', {'profile_id': profile['id'], 'host_task': str(host_task),
                  'run_id': run_id, 'started_at': started_at, 'phase': 'preparing'})
        try:
            if preparation_error:
                raise RuntimeError(preparation_error)
            llvm_file = source / 'triton/cmake/llvm-hash.txt'
            if llvm_file.exists() and llvm_file.read_text().strip() != profile.get('llvm_revision'):
                raise RuntimeError('tested LLVM revision lacks a matching trusted environment recipe/profile')
            if task.get('triton_version') and task['triton_version'] != profile['triton_version']:
                raise RuntimeError('task version differs from the trusted branch profile')
            self.heartbeat('preparing', task['task_id'])
            self.docker_run(profile, 'chown', '-R', '1000:1000', container_task)
            self.docker_run(profile, 'chown', '0:0', container_task)
            self.docker_run(profile, 'chmod', '755', container_task)
            self.docker_run(profile, 'chmod', '-R', 'u+rwX,go+rX,go-w', container_task + '/source')
            self.docker_run(profile, 'chmod', '2750', container_task + '/artifacts')
            self.docker_run(profile, 'chmod', '2770', container_task + '/artifacts/custom')
            self.docker_run(profile, 'chown', '-R', profile.get('agent_user', '1001:1000'), container_task + '/agent')
            self.docker_run(profile, 'chmod', '700', container_task + '/agent')
            if not (host_task / 'venv').exists():
                self.docker_run(profile, 'cp', '-a', profile.get('seed_venv', '/opt/ci-venv'), container_task + '/venv')
            self.docker_run(profile, 'chown', '-R', '1000:1000', container_task + '/venv')
            from .performance import prepare_baselines
            context['performance_baselines'] = prepare_baselines(self.config, profile, task, host_task, container_task)
            if (host_task / 'baselines').exists():
                self.docker_run(profile, 'chown', '-R', '0:0', container_task + '/baselines')
                self.docker_run(profile, 'chmod', '-R', 'a+rX,go-w,u-w', container_task + '/baselines')
            home = self.prepare_agent_home(profile)
            # Profile-mounted dependency sources are trusted, not fetched from PR .gitmodules.
            for name, location in profile.get('dependency_sources', {}).items():
                if name not in ('triton', 'FlagGems') or not location.startswith('/'):
                    raise ValueError('unsupported trusted dependency source')
                expected = tracked_hashes.get(name, '')
                if not expected.startswith('gitlink:'):
                    raise ValueError('dependency source may only materialize an exact tracked gitlink')
                expected_sha = expected.split(':', 1)[1]
                if not (source / name / '.git').exists():
                    self.docker_run(profile, 'git', '-c', 'core.hooksPath=/dev/null', 'clone',
                        '--no-hardlinks', '--no-recurse-submodules', '--no-checkout', '--',
                        location, container_task + '/source/' + name)
                    self.docker_run(profile, 'git', '-C', container_task + '/source/' + name,
                                    'checkout', '--detach', expected_sha)
                    self.docker_run(profile, 'chown', '-R', '1000:1000', container_task + '/source/' + name)
                if git(source / name, '-c', 'safe.directory=' + str((source / name).resolve()), 'rev-parse', 'HEAD') != expected_sha:
                    raise ValueError('materialized dependency differs from the tested gitlink')
                if name == 'FlagGems':
                    profile.setdefault('tools', {})['flaggems_dir'] = container_task + '/source/FlagGems'
            write_json(host_task / 'agent/context.json', {'task': task, 'policy': policy,
                       'source_dir': context['source_dir'], 'artifact_dir': context['artifact_dir']})
            if verify_control(self.config, task)['tree_sha256'] != control_identity['tree_sha256']:
                raise RuntimeError('trusted control files changed before Codex execution')
            verify_container_control(self.config, profile, control_identity)
            port = broker.start()
            environment = os.environ.copy()
            environment.update({'LOCAL_CI_BROKER_URL': f"http://{self.config.get('broker_host', 'host.docker.internal')}:{port}/",
                                'LOCAL_CI_BROKER_TOKEN': broker.token, 'CODEX_HOME': home})
            settings = self.config['codex']
            prompt = ('Read /opt/anchor-ci/ai_ci_program.md and ' + container_task +
                      '/agent/context.json. Execute the current CI task through the broker, '
                      'review the frozen source, and submit your review with finalize. '
                      'Keep all working notes and generated tests in the artifact directory.')
            if resume_record:
                prompt += ' This task was interrupted. Resume from the saved plan and host receipts; do not repeat completed applicable checks.'
            codex_args = [settings.get('bin', 'codex'), 'exec', '--json', '--skip-git-repo-check', '--ignore-user-config',
                          '--sandbox', 'danger-full-access', '-c', 'approval_policy="never"',
                          '-C', container_task + '/agent', '--output-schema',
                          '/opt/anchor-ci/schemas/completion.schema.json', '-o',
                          container_task + '/agent/completion.json']
            if settings.get('model'):
                codex_args += ['-m', settings['model']]
            if settings.get('reasoning_effort'):
                codex_args += ['-c', 'model_reasoning_effort=' + json.dumps(settings['reasoning_effort'])]
            codex_args.append(prompt)
            spec = {'id': task['task_id'] + '-codex', 'argv': codex_args,
                    'cwd': container_task + '/agent', 'env': {}}
            encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
            prefix = [self.docker, 'exec', '--user', profile.get('agent_user', '1001:1000'),
                      '-e', 'LOCAL_CI_BROKER_URL', '-e', 'LOCAL_CI_BROKER_TOKEN', '-e', 'CODEX_HOME',
                      profile['container']['name'], '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py']
            def stop_codex():
                subprocess.run(prefix + ['stop', encoded], env=environment, capture_output=True, timeout=15)
            def monitor():
                while not stopped.wait(self.config.get('validity_interval', 10)):
                    self.heartbeat('running', task['task_id'])
                    try:
                        if not self.relay.current(task):
                            cancelled.set()
                            return
                        monitor_errors.clear()
                    except Exception as exc:
                        monitor_errors.append(str(exc))
                        if len(monitor_errors) >= 3:
                            cancelled.set()
                            return
            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            write_json(output / 'execution.json', {'profile_id': profile['id'], 'host_task': str(host_task),
                       'run_id': run_id, 'started_at': started_at, 'phase': 'running'})
            self.heartbeat('running', task['task_id'])
            outcome = execute(prefix + ['run', encoded], output / 'agent-events.jsonl', env=environment,
                              timeout=settings.get('timeout', 14400), cancelled=cancelled.is_set,
                              terminate=stop_codex)
            exitcode = outcome['returncode']
            if outcome['termination']:
                failure = 'Codex ' + outcome['termination']
            if monitor_errors and cancelled.is_set():
                failure = 'task validity could not be verified'
        except Exception as exc:
            failure = str(exc)
        finally:
            stopped.set()
            broker.stop()
            if lease:
                try:
                    self.docker_run(profile, '/usr/bin/python3', '-I',
                                    '/opt/anchor-ci/runtime/container_process.py', 'clean-users')
                    self.manager.release(profile, task['task_id'])
                except Exception as exc:
                    failure = f'worker cleanup failed; lease retained: {exc}'
        def unchanged(path, expected):
            candidate = source / path
            if expected.startswith('symlink:'):
                return candidate.is_symlink() and os.readlink(candidate) == expected[8:]
            if expected.startswith('gitlink:'):
                if not (candidate / '.git').exists():
                    return candidate.is_dir() and not any(candidate.iterdir())
                trusted = ['-c', 'safe.directory=' + str(candidate.resolve())]
                return (git(candidate, *trusted, 'rev-parse', 'HEAD') == expected[8:] and
                        not git(candidate, *trusted, 'status', '--porcelain', '--untracked-files=no'))
            return candidate.is_file() and not candidate.is_symlink() and digest(candidate) == expected
        try:
            source_unchanged = bool(tracked_hashes) and all(unchanged(path, expected) for path, expected in tracked_hashes.items())
        except (OSError, RuntimeError):
            source_unchanged = False
        try:
            if not self.relay.current(task):
                cancelled.set()
            final_control = verify_control(self.config, task)
            if final_control.get('tree_sha256') != control_identity.get('tree_sha256'):
                raise RuntimeError('trusted control files changed during task execution')
            verify_container_control(self.config, profile, control_identity)
        except Exception:
            failure = failure or 'final task validity check unavailable'
        result = build_result(task, run_id, policy, broker, started_at=started_at,
                              source_unchanged=source_unchanged, agent_exitcode=exitcode,
                              cancelled=cancelled.is_set(), error=failure)
        result['environment'] = {'profile_id': profile['id'], 'triton_version': profile['triton_version'],
                                 'llvm_revision': profile.get('llvm_revision'),
                                 'selection': profile.get('llvm_selection')}
        from .artifacts import collect_artifacts
        try:
            result['artifacts'] = collect_artifacts(host_task / 'artifacts', output)
        except OSError as exc:
            result['blocking_reasons'].append('artifact preservation failed: ' + str(exc))
            result['conclusion'] = 'error'
        write_json(output / 'result.json', result)
        (output / 'report.md').write_text(markdown(result), encoding='utf-8')
        write_json(output / 'execution.json', {'profile_id': profile['id'], 'host_task': str(host_task),
                   'run_id': run_id, 'started_at': started_at, 'phase': 'publish_pending'})
        self.heartbeat('publish_pending', task['task_id'], failure)
        return output, result

    def recover_publication(self, record, diagnostic):
        """Rejoin the original Codex session for publication diagnosis, without tests.

        The host retains credentials and immutable results. This bounded follow-up
        gives Codex the saved failure evidence; only the publisher retries the push.
        """
        execution = read_json(record)
        if execution.get('publication_recovery_attempts', 0) >= self.config['codex'].get('publication_recovery_attempts', 1):
            return
        host_task = execution.get('host_task')
        if not host_task:
            return
        event_path = Path(record).parent / 'agent-events.jsonl'
        session_id = None
        if event_path.exists():
            for line in event_path.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started':
                    session_id = event.get('thread_id')
                    break
        if not session_id or not re.fullmatch(r'[0-9a-f-]{36}', session_id):
            return
        task = read_json(Path(record).parent / 'task.json')
        profile = next(p for p in self.config['profiles'] if p['id'] == execution['profile_id'])
        if not self.config.get('local_acceptance'):
            profile = self.manager.effective_profile(profile)
        self.manager.acquire(profile, task['task_id'])
        try:
            from .control import verify_control, verify_container_control
            verify_container_control(self.config, profile, verify_control(self.config, task))
            execution['publication_recovery_attempts'] = execution.get('publication_recovery_attempts', 0) + 1
            write_json(record, execution)
            safe_diagnostic = re.sub(r'https?://[^/\s@]+@', 'https://REDACTED@', str(diagnostic))[-6000:]
            agent_dir = Path(host_task) / 'agent'
            write_json(agent_dir / 'publication-diagnostic.json', {'task_id': task['task_id'],
                       'phase': 'publish_pending', 'diagnostic': safe_diagnostic,
                       'instruction': 'Build/review already complete. Diagnose publication only. Do not rerun tests, alter result files, or request credentials. Host retries publication.'})
            container_dir = '/workspace/' + agent_dir.relative_to(self.workspace).as_posix()
            settings = self.config['codex']
            prompt = ('Continue this CI task for publication recovery. Read ' + container_dir +
                      '/publication-diagnostic.json. Explain the likely cause and a bounded recovery '
                      'recommendation in Chinese. Preserve completed test results. The host owns '
                      'credentials and retries publication; no build/test tools are available now.')
            command = [settings.get('bin', 'codex'), 'exec', 'resume', '--json', '--ignore-user-config',
                       '--skip-git-repo-check', '-c', 'approval_policy="never"', '-c', 'sandbox_mode="danger-full-access"',
                       session_id, prompt]
            spec = {'id': task['task_id'] + '-publish', 'argv': command, 'cwd': container_dir, 'env': {}}
            encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
            env = os.environ.copy()
            env['CODEX_HOME'] = settings.get('home', '/home/agent/.codex')
            prefix = [self.docker, 'exec', '--user', profile.get('agent_user', '1001:1000'), '-e', 'CODEX_HOME',
                      profile['container']['name'], '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py']
            self.heartbeat('publish_pending', task['task_id'], safe_diagnostic)
            execute(prefix + ['run', encoded], Path(record).parent / 'publication-recovery.jsonl', env=env,
                    timeout=settings.get('publication_recovery_timeout', 180),
                    terminate=lambda: subprocess.run(prefix + ['stop', encoded], env=env, capture_output=True, timeout=15))
        finally:
            self.docker_run(profile, '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py', 'clean-users')
            self.manager.release(profile, task['task_id'])
