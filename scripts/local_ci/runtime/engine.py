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
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from maintenance.workers import WorkerBusy

from .broker import Broker
from .common import digest, execute, git, read_json, utcnow, write_json
from .codex_events import read_events
from .policy import minimum_checks, validate_task
from .report import build_result, build_source_index, markdown, validate_source_index
from .result_paths import run_relative, validate_result_path
from .task_permissions import write_agent_document


class Engine:
    @staticmethod
    def codex_provider(settings):
        """Validate the explicitly selected host-owned Responses provider."""
        provider = settings.get('provider')
        if provider is None:
            return None
        if not isinstance(provider, dict) or set(provider) - {'id', 'name', 'base_url', 'env_key', 'wire_api'}:
            raise ValueError('codex.provider must contain only id, name, base_url, env_key and wire_api')
        if not isinstance(provider.get('id'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', provider['id']):
            raise ValueError('codex.provider.id must be a provider identifier')
        name = provider.get('name', provider['id'])
        if not isinstance(name, str) or not name.strip() or not name.isprintable():
            raise ValueError('codex.provider.name must be a nonempty printable name')
        env_key = provider.get('env_key')
        if not isinstance(env_key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*_API_KEY', env_key):
            raise ValueError('codex.provider.env_key must name an uppercase API_KEY environment variable')
        if provider.get('wire_api', 'responses') != 'responses':
            raise ValueError('codex.provider.wire_api must be responses')
        base_url = provider.get('base_url')
        try:
            if (not isinstance(base_url, str) or not base_url.isascii() or not base_url.isprintable() or
                    any(c.isspace() for c in base_url)):
                raise ValueError
            parsed = urlsplit(base_url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or
                    parsed.password is not None or parsed.query or parsed.fragment or
                    '?' in base_url or '#' in base_url or '\\' in base_url or
                    not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)):
                raise ValueError
            parsed.port  # Reject malformed/out-of-range ports before spawning the CLI.
        except ValueError:
            raise ValueError('codex.provider.base_url must be HTTPS without credentials, query or fragment') from None
        return {'id': provider['id'], 'name': name, 'base_url': base_url, 'env_key': env_key,
                'wire_api': 'responses'}

    @staticmethod
    def codex_options(settings):
        """Apply the same trusted model and history budget on every CLI entry."""
        options = []
        if settings.get('model'):
            options += ['-m', settings['model']]
        if settings.get('reasoning_effort'):
            options += ['-c', 'model_reasoning_effort=' + json.dumps(settings['reasoning_effort'])]
        provider = Engine.codex_provider(settings)
        if provider:
            options += ['-c', 'model_provider=' + json.dumps(provider['id'])]
            for key in ('name', 'base_url', 'env_key', 'wire_api'):
                options += ['-c', 'model_providers.' + provider['id'] + '.' + key + '=' + json.dumps(provider[key])]
            # The CLI needs the key, but agent shell commands and tests do not.
            options += ['-c', 'shell_environment_policy.exclude=' + json.dumps([provider['env_key']])]
            # Login startup/saved shell snapshots can restore the filtered key.
            options += ['-c', 'allow_login_shell=false', '-c', 'features.shell_snapshot=false']
        if 'model_catalog_json' in settings:
            catalog = settings['model_catalog_json']
            if (not isinstance(catalog, str) or not catalog.startswith('/') or catalog.startswith('//') or
                    not catalog.isprintable() or '\\' in catalog or
                    PurePosixPath(catalog).as_posix() != catalog or '..' in PurePosixPath(catalog).parts):
                raise ValueError('codex.model_catalog_json must be a canonical absolute container path')
            options += ['-c', 'model_catalog_json=' + json.dumps(catalog)]
        for key in ('auto_compact_token_limit', 'tool_output_token_limit'):
            if key in settings:
                value = settings[key]
                if type(value) is not int or value <= 0:
                    raise ValueError('codex.' + key + ' must be a positive integer')
                cli_key = 'model_' + key if key == 'auto_compact_token_limit' else key
                options += ['-c', cli_key + '=' + str(value)]
        return options

    @staticmethod
    def codex_environment(settings):
        environment = os.environ.copy()
        provider = Engine.codex_provider(settings)
        if provider and not environment.get(provider['env_key'], '').strip():
            raise ValueError('Codex provider credential is missing: ' + provider['env_key'])
        return environment

    def codex_prefix(self, profile, *environment_keys):
        prefix = [self.docker, 'exec', '--user', profile.get('agent_user', '1001:1000')]
        provider = self.codex_provider(self.config['codex'])
        for key in (*environment_keys, *((provider['env_key'],) if provider else ())):
            prefix += ['-e', key]
        return prefix + [profile['container']['name'], '/usr/bin/python3', '-I',
                         '/opt/anchor-ci/runtime/container_process.py']

    def __init__(self, config, relay, manager):
        self.config, self.relay, self.manager = config, relay, manager
        self.state = Path(config['state_dir'])
        self.workspace = Path(config['workspace_host'])
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.docker = config.get('docker', 'docker')
        self.preparation_record = None
        self.preparation_task_id = None
        self.preparation_cleanup_failed = False

    def heartbeat(self, phase, task_id=None, error=None):
        record = {'heartbeat_at': time.time(), 'state': phase, 'task_id': task_id, 'error': error}
        write_json(self.state / 'health/poller.json', record)
        if task_id:
            write_json(self.state / 'health/task.json', {**record, 'phase': phase,
                       'started_at': getattr(self, 'task_started', time.time())})

    def stop_preparation(self, profile, saved):
        """Stop only this trusted invocation, including a delayed Docker exec."""
        if saved['container'] != profile['container']['name'] or saved['profile_id'] != profile['id']:
            raise RuntimeError('saved preparation belongs to another worker')
        payload = base64.urlsafe_b64encode(json.dumps(saved['spec']).encode()).decode()
        stopped = subprocess.run([self.docker, 'exec', '--user', saved['user'], saved['container'],
            '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py', 'stop', payload],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if stopped.returncode:
            raise RuntimeError('worker preparation cleanup could not be confirmed')

    def docker_run(self, profile, *args, user='0'):
        timeout = self.config.get('preparation_timeout', 300)
        if type(timeout) is not int or timeout <= 0:
            raise ValueError('preparation_timeout must be a positive integer')
        if self.preparation_cleanup_failed:
            raise RuntimeError('worker preparation cleanup failed; lease retained')
        spec = {'id': 'prepare-' + secrets.token_hex(16), 'argv': list(args), 'cwd': '/', 'timeout': timeout}
        saved = {'profile_id': profile['id'], 'container': profile['container']['name'], 'user': user, 'spec': spec}
        if self.preparation_record:
            write_json(self.preparation_record, saved)
        payload = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
        argv = [self.docker, 'exec', '--user', user, saved['container'], '/usr/bin/python3', '-I',
                '/opt/anchor-ci/runtime/container_process.py', 'run', payload]
        process = None
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + timeout + 30
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('worker preparation', timeout + 30)
                try:
                    stdout, stderr = process.communicate(timeout=min(20, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if self.preparation_task_id:
                        self.heartbeat('preparing', self.preparation_task_id)
            if process.returncode:
                raise RuntimeError('worker preparation failed: ' + stderr[-1000:])
        except BaseException:
            try:
                self.stop_preparation(profile, saved)
            except Exception:
                self.preparation_cleanup_failed = True
                raise RuntimeError('worker preparation cleanup unconfirmed; lease retained') from None
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
            if self.preparation_record:
                self.preparation_record.unlink(missing_ok=True)
            raise
        if self.preparation_record:
            self.preparation_record.unlink(missing_ok=True)
        return stdout

    def prepare_agent_home(self, profile):
        settings = self.config['codex']
        agent_home = settings.get('home', '/home/agent/.codex')
        self.docker_run(profile, 'mkdir', '-p', agent_home)
        # Copy only explicitly selected authentication, never the desktop config/plugins.
        if settings.get('auth_file'):
            subprocess.run([self.docker, 'cp', settings['auth_file'],
                profile['container']['name'] + ':' + agent_home + '/auth.json'],
                check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.docker_run(profile, 'chown', '-R', profile.get('agent_user', '1001:1000'), agent_home)
        self.docker_run(profile, 'chmod', '700', agent_home)
        return agent_home

    def prepare_venv(self, profile, host_task, container_task, output):
        """A directory alone does not prove an interrupted seed copy completed."""
        marker = output / 'venv-ready.json'
        expected = {'profile_id': profile['id'], 'seed': profile.get('seed_venv', '/opt/ci-venv'),
                    'path': container_task + '/venv'}
        target = host_task / 'venv'
        if target.exists() or marker.exists():
            if target.is_symlink() or not target.is_dir() or not marker.is_file() or read_json(marker) != expected:
                raise RuntimeError('task environment copy is incomplete; preserve this run and dispatch a new task')
            return
        self.docker_run(profile, 'cp', '-a', expected['seed'], expected['path'])
        self.docker_run(profile, 'chown', '-R', '1000:1000', expected['path'])
        write_json(marker, expected)

    def execute_codex(self, spec, prefix, environment, output, cancelled, validate):
        """Keep the broker/lease alive across at most two transient service errors."""
        settings = self.config['codex']
        deadline = time.monotonic() + settings.get('timeout', 14400)
        original = copy.deepcopy(spec)
        attempts, session_id = [], None
        failure, outcome = None, {'returncode': 1, 'termination': None}
        for attempt in range(3):
            if cancelled.is_set():
                failure = 'Codex cancelled'
                break
            if time.monotonic() >= deadline:
                failure = 'Codex timeout: original execution budget exhausted'
                break
            if attempt:
                # The same frozen task and trusted control must still be valid.
                try:
                    validate()
                except Exception as exc:
                    failure = 'Codex recovery validation failed: ' + str(exc)
                    break
                if cancelled.is_set():
                    failure = 'Codex cancelled'
                    break
                prompt = ('A transient Codex service failure interrupted this CI task. '
                          'The same task, frozen source, broker, worker lease and running build are retained. '
                          'Read /opt/anchor-ci/ai_ci_program.md and ' + original['cwd'] +
                          '/context.json. First inspect broker status and wait for any active command; '
                          'status=running means continue review or wait 30-60 seconds before polling again. '
                          'Do not repeat active or completed checks. Continue the saved plan and host '
                          'receipts, then submit the review through finalize. If already finalized, '
                          'preserve that review and finish. No extra execution budget was granted.')
                if session_id:
                    command = [settings.get('bin', 'codex'), 'exec', 'resume', '--json',
                               '--ignore-user-config', '--skip-git-repo-check',
                               '-c', 'approval_policy="never"', '-c', 'sandbox_mode="danger-full-access"']
                    command += self.codex_options(settings)
                    command += [session_id, prompt]
                else:
                    # Failure before thread.started: use the same on-disk task
                    # context and broker receipts, without claiming a resumed session.
                    command = original['argv'][:-1] + [original['argv'][-1] + '\n' + prompt]
                spec = {**original, 'id': original['id'] + f'-recovery-{attempt}', 'argv': command}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = 'Codex timeout: original execution budget exhausted'
                break
            encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
            log = output / ('agent-events.jsonl' if not attempt else f'agent-events-recovery-{attempt}.jsonl')
            try:
                outcome = execute(prefix + ['run', encoded], log, env=environment, timeout=remaining,
                    cancelled=cancelled.is_set, terminate=lambda encoded=encoded: subprocess.run(
                        prefix + ['stop', encoded], env=environment, capture_output=True, timeout=15,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
            except Exception as exc:
                failure = 'Codex execution failed: ' + str(exc)
                attempts.append({'attempt': attempt, 'log_path': log.name, 'error': failure})
                break
            observed_session, error = read_events(log)
            session_id = observed_session or session_id
            failure = ('Codex ' + outcome['termination'] if outcome['termination'] else
                       'Codex service error: ' + error.get('message', error.get('code', 'turn failed')) if error else
                       f"Codex exited with code {outcome['returncode']}" if outcome['returncode'] else None)
            record = {'attempt': attempt, 'session_id': session_id, 'log_path': log.name,
                      'service_error': error, **outcome}
            attempts.append(record)
            retry = bool(error and error['retryable'] and outcome['returncode'] and
                         not outcome['termination'] and not cancelled.is_set() and attempt < 2)
            if retry:
                delay = (30, 60)[attempt]
                if deadline - time.monotonic() <= delay:
                    failure += '; recovery skipped: insufficient remaining execution budget'
                    retry = False
                else:
                    record['backoff_seconds'] = delay
            if error and error['retryable'] and attempt == 2:
                failure += '; two service recoveries exhausted'
            write_json(output / 'agent-recovery.json', {'attempts': attempts, 'error': failure,
                       'state': 'backoff' if retry else 'finished'})
            if not retry:
                break
            if cancelled.wait(delay):
                failure = 'Codex cancelled during service recovery'
                break
        write_json(output / 'agent-recovery.json', {'attempts': attempts, 'error': failure, 'state': 'finished'})
        return outcome['returncode'], failure

    def run(self, task, profile, resume_record=None):
        validate_task(task, self.config.get('repository', 'anteloper-c/triton-anchor'))
        self.preparation_record = None
        self.preparation_task_id = None
        self.preparation_cleanup_failed = False
        if resume_record:
            validate_result_path(Path(resume_record).relative_to(self.state).as_posix(), task,
                                 read_json(resume_record)['run_id'], 'execution.json')
            pending = Path(resume_record).with_name('preparation.json')
            if pending.exists():
                # Poller released the interrupted lease; reserve it again before
                # touching the exact root process or any partially copied files.
                self.manager.acquire(profile, task['task_id'])
                self.stop_preparation(profile, read_json(pending))
                pending.unlink()
                self.manager.release(profile, task['task_id'])
        configured_profile = profile
        profile = copy.deepcopy(profile)
        # Acquire before creating an execution journal: maintenance/busy means queued.
        try:
            self.codex_options(self.config['codex'])
            environment = self.codex_environment(self.config['codex'])
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
            output = Path(resume_record).parent if resume_record else self.state / run_relative(task, run_id)
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
        output = Path(resume_record).parent if resume_record else self.state / run_relative(task, run_id)
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
        source_index = None
        try:
            if resume_record:
                tracked_hashes = read_json(output / 'source-manifest.json')
                source_index = read_json(output / 'source-index.json')
                validate_source_index(source_index, tracked_hashes)
            else:
                tracked_hashes = self.relay.checkout(task['tested_sha'], source)
                write_json(output / 'source-manifest.json', tracked_hashes)
                source_index = build_source_index(source, tracked_hashes)
                write_json(output / 'source-index.json', source_index)
        except Exception as exc:
            preparation_error = str(exc)
        for directory in ('artifacts/custom', 'agent'):
            (host_task / directory).mkdir(parents=True, exist_ok=True)
        container_task = '/workspace/tasks/' + task['task_id'] + '/' + run_id
        context = {'task_id': task['task_id'], 'target_sha': task['tested_sha'],
                   'target_branch': task['target_branch'],
                   'event_kind': task['event_kind'],
                   'control_identity': control_identity,
                   'validation_scope': 'local_acceptance' if self.config.get('local_acceptance') else 'production',
                   'source_dir': container_task + '/source', 'source_host_dir': str(source),
                   'source_index': source_index,
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
        self.preparation_record = output / 'preparation.json'
        self.preparation_task_id = task['task_id']
        try:
            if preparation_error:
                raise RuntimeError(preparation_error)
            llvm_file = source / 'triton/cmake/llvm-hash.txt'
            if llvm_file.exists() and llvm_file.read_text().strip() != profile.get('llvm_revision'):
                raise RuntimeError('tested LLVM revision lacks a matching trusted environment recipe/profile')
            if task.get('triton_version') and task['triton_version'] != profile['triton_version']:
                raise RuntimeError('task version differs from the trusted branch profile')
            self.heartbeat('preparing', task['task_id'])
            self.docker_run(profile, '/usr/bin/python3', '-I', '-S',
                            '/opt/anchor-ci/runtime/task_permissions.py', container_task)
            self.docker_run(profile, 'chown', '-R', '1000:1000', container_task)
            self.docker_run(profile, 'chown', '0:0', container_task)
            self.docker_run(profile, 'chmod', '755', container_task)
            self.docker_run(profile, 'chmod', '-R', 'u+rwX,go+rX,go-w', container_task + '/source')
            self.docker_run(profile, 'chmod', '2750', container_task + '/artifacts')
            self.docker_run(profile, 'chmod', '2770', container_task + '/artifacts/custom')
            self.docker_run(profile, 'chown', '-R', profile.get('agent_user', '1001:1000'), container_task + '/agent')
            self.docker_run(profile, 'chmod', '700', container_task + '/agent')
            self.prepare_venv(profile, host_task, container_task, output)
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
            write_agent_document(host_task / 'agent/context.json', {'task': task, 'policy': policy,
                       'source_dir': context['source_dir'], 'artifact_dir': context['artifact_dir']})
            if verify_control(self.config, task)['tree_sha256'] != control_identity['tree_sha256']:
                raise RuntimeError('trusted control files changed before Codex execution')
            verify_container_control(self.config, profile, control_identity)
            port = broker.start()
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
            codex_args += self.codex_options(settings)
            codex_args.append(prompt)
            spec = {'id': task['task_id'] + '-codex', 'argv': codex_args,
                    'cwd': container_task + '/agent',
                    'env': {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'safe.directory',
                            'GIT_CONFIG_VALUE_0': context['source_dir']}}
            prefix = self.codex_prefix(profile, 'LOCAL_CI_BROKER_URL', 'LOCAL_CI_BROKER_TOKEN', 'CODEX_HOME')
            def validate_recovery():
                if not self.relay.current(task):
                    cancelled.set()
                    return
                if verify_control(self.config, task)['tree_sha256'] != control_identity['tree_sha256']:
                    raise RuntimeError('trusted control files changed before Codex recovery')
                verify_container_control(self.config, profile, control_identity)
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
            exitcode, failure = self.execute_codex(spec, prefix, environment, output, cancelled, validate_recovery)
            if monitor_errors and cancelled.is_set():
                failure = 'task validity could not be verified'
        except Exception as exc:
            failure = str(exc)
        finally:
            stopped.set()
            broker.stop()
            if lease:
                try:
                    if self.preparation_cleanup_failed:
                        raise RuntimeError('root preparation cleanup is unconfirmed')
                    self.docker_run(profile, '/usr/bin/python3', '-I',
                                    '/opt/anchor-ci/runtime/container_process.py', 'clean-users')
                    self.manager.release(profile, task['task_id'])
                except Exception as exc:
                    failure = f'worker cleanup failed; lease retained: {exc}'
            self.preparation_record = None
            self.preparation_task_id = None
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
        session_id = None
        for event_path in [Path(record).parent / name for name in (
                'agent-events.jsonl', 'agent-events-recovery-1.jsonl', 'agent-events-recovery-2.jsonl')]:
            if event_path.exists():
                observed, _ = read_events(event_path)
                session_id = observed or session_id
        if not session_id:
            return
        task = read_json(Path(record).parent / 'task.json')
        profile = next(p for p in self.config['profiles'] if p['id'] == execution['profile_id'])
        if not self.config.get('local_acceptance'):
            profile = self.manager.effective_profile(profile)
        env = self.codex_environment(self.config['codex'])
        self.codex_options(self.config['codex'])
        self.manager.acquire(profile, task['task_id'])
        try:
            from .control import verify_control, verify_container_control
            verify_container_control(self.config, profile, verify_control(self.config, task))
            execution['publication_recovery_attempts'] = execution.get('publication_recovery_attempts', 0) + 1
            write_json(record, execution)
            safe_diagnostic = re.sub(r'https?://[^/\s@]+@', 'https://REDACTED@', str(diagnostic))[-6000:]
            agent_dir = Path(host_task) / 'agent'
            write_agent_document(agent_dir / 'publication-diagnostic.json', {'task_id': task['task_id'],
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
                       ]
            command += self.codex_options(settings)
            command += [session_id, prompt]
            spec = {'id': task['task_id'] + '-publish', 'argv': command, 'cwd': container_dir, 'env': {}}
            encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
            env['CODEX_HOME'] = settings.get('home', '/home/agent/.codex')
            prefix = self.codex_prefix(profile, 'CODEX_HOME')
            self.heartbeat('publish_pending', task['task_id'], safe_diagnostic)
            execute(prefix + ['run', encoded], Path(record).parent / 'publication-recovery.jsonl', env=env,
                    timeout=settings.get('publication_recovery_timeout', 180),
                    terminate=lambda: subprocess.run(prefix + ['stop', encoded], env=env, capture_output=True, timeout=15,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        finally:
            self.docker_run(profile, '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py', 'clean-users')
            self.manager.release(profile, task['task_id'])
