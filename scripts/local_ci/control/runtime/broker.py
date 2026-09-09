"""Host-owned tool execution and evidence broker for a single worker lease."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

from .common import digest, execute, read_json, utcnow, write_json
from .policy import TOOLS
from .report import validate_architecture_evidence
from tools.basic_tools.runner import DEPENDENCIES


class Broker:
    def __init__(self, profile, context, policy, output, cancelled, *, docker='docker',
                 max_commands=100, max_seconds=14400):
        self.profile, self.context, self.policy = profile, context, policy
        self.output = Path(output)
        self.cancelled, self.docker = cancelled, docker
        self.max_commands, self.max_seconds = max_commands, max_seconds
        self.token = secrets.token_urlsafe(32)
        self.receipts, self.checks = [], {}
        self.lock = threading.RLock()
        self.active = None
        self.active_command = None
        self.closed = False
        self.review = None
        self.artifact_fingerprints = {}
        self.performance = []
        if (self.output / 'performance.json').exists():
            self.performance = read_json(self.output / 'performance.json')
        if (self.output / 'evidence.json').exists():
            self.receipts = read_json(self.output / 'evidence.json')
        if (self.output / 'checks.json').exists():
            self.checks = {c['id']: c for c in read_json(self.output / 'checks.json')}
        if (self.output / 'artifact-fingerprints.json').exists():
            self.artifact_fingerprints = read_json(self.output / 'artifact-fingerprints.json')
        if (self.output / 'active-command.json').exists():
            active_tool = read_json(self.output / 'active-command.json')['tool']
            invalidated = {active_tool}
            while True:
                following = {name for name, deps in DEPENDENCIES.items() if set(deps) & invalidated}
                if following <= invalidated:
                    break
                invalidated |= following
            for name in invalidated:
                self.checks.pop(name, None)
                self.artifact_fingerprints.pop(name, None)
            self.performance = [item for item in self.performance if item['tool'] not in invalidated]

    def command(self, tool, command):
        if self.cancelled() or self.closed:
            raise RuntimeError('task is cancelled or finalized')
        if len(self.receipts) >= self.max_commands:
            raise RuntimeError('command budget exhausted')
        used = sum(r['elapsed_seconds'] for r in self.receipts)
        if used >= self.max_seconds:
            raise RuntimeError('execution budget exhausted')
        receipt_id = f'command-{len(self.receipts)+1:04d}'
        spec = {'id': self.context['task_id'] + '-' + receipt_id, **command}
        spec['env'] = {**self.profile.get('tools', {}).get('env', {}), **spec.get('env', {})}
        if self.context.get('task_venv'):
            spec['env']['LOCAL_CI_TASK_VENV'] = self.context['task_venv']
        # Credentials and broker privileges are not inherited by build/test commands.
        for key in ('LOCAL_CI_BROKER_TOKEN', 'LOCAL_CI_BROKER_URL', 'OPENAI_API_KEY', 'GITEE_TOKEN', 'GITHUB_TOKEN'):
            spec['env'][key] = ''
        encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
        prefix = [self.docker, 'exec', '--user', str(self.profile.get('test_user', '1000:1000')),
                  self.profile['container']['name'], '/usr/bin/python3', '-I', '/opt/anchor-ci/control/runtime/container_process.py']
        argv = prefix + ['run', encoded]
        def stop():
            subprocess.run(prefix + ['stop', encoded], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.active = stop
        log = self.output / 'logs' / (receipt_id + '.log')
        started = utcnow()
        self.active_command = {'id': receipt_id, 'tool': tool, 'started_at': started}
        write_json(self.output / 'active-command.json', self.active_command)
        result = execute(argv, log, timeout=min(command.get('timeout', 900), self.max_seconds-used),
                         cancelled=self.cancelled, terminate=stop)
        self.active = None
        self.active_command = None
        receipt = {'id': receipt_id, 'tool': tool, 'argv': command['argv'],
                   'cwd': command['cwd'], 'started_at': started, 'completed_at': utcnow(),
                   'log_path': 'logs/' + log.name, **result}
        self.receipts.append(receipt)
        write_json(self.output / 'evidence.json', self.receipts)
        (self.output / 'active-command.json').unlink(missing_ok=True)
        return receipt

    def verify_artifacts(self, tool):
        from .relay import evidence_path
        root = self.context.get('artifact_host_dir')
        if not root or tool in ('custom_test', 'control_plane'):
            return
        required = {
            'environment': ['environment.json'], 'frontend_build': ['wheel.json'],
            'frontend_install': ['installation.json'], 'frontend_tests': ['tests.xml', 'tests.json'],
            'frontend_smoke': ['smoke_success.json'], 'backend_build': ['wheel.json'],
            'backend_install': ['installation.json'], 'backend_tests': ['tests.xml', 'tests.json'],
            'backend_smoke': ['smoke_success.json'],
            'flaggems': ['flaggems-summary.json', 'selected.txt'],
            **{name: ['candidate.json', 'comparison.json', 'baseline_identity.json']
               for name in ('compile_time', 'pass_profile', 'ir_serialization')},
        }
        documents = {}
        for name in required.get(tool, []):
            path = evidence_path(root, tool + '/' + name)
            if not path.is_file():
                raise ValueError(f'{tool} exited zero without its required artifact {name}')
            if path.suffix == '.json':
                documents[name] = read_json(path)
                if not isinstance(documents[name], dict):
                    raise ValueError('tool artifact must be a JSON object')
        if tool in ('environment', 'frontend_build', 'frontend_install', 'frontend_tests', 'frontend_smoke',
                    'backend_build', 'backend_install', 'backend_tests', 'backend_smoke'):
            for document in documents.values():
                if any(document.get(key) != self.context[key] for key in ('task_id', 'target_sha')):
                    raise ValueError('tool artifact identity differs from current task')
        if 'wheel.json' in documents:
            manifest = documents['wheel.json']
            prefix = self.context['artifact_dir'].rstrip('/') + '/'
            wheel = manifest.get('wheel', '')
            if not isinstance(wheel, str) or not wheel.startswith(prefix + tool + '/wheels/'):
                raise ValueError('wheel manifest path is outside this build')
            actual = evidence_path(root, wheel[len(prefix):])
            if not actual.is_file() or digest(actual) != manifest.get('sha256'):
                raise ValueError('wheel manifest has no matching actual artifact')
        for name in ('installation.json', 'smoke_success.json', 'tests.json'):
            if name in documents:
                document = documents[name]
                if (document.get('python_executable') != self.context.get('python_bin', 'python3')
                        or document.get('task_venv') != self.context.get('task_venv')):
                    raise ValueError('tool artifact belongs to a different Python environment')
        if 'installation.json' in documents:
            build = 'backend_build' if tool == 'backend_install' else 'frontend_build'
            built = read_json(evidence_path(root, build + '/wheel.json'))
            if any(documents['installation.json'].get(key) != built.get(key)
                   for key in ('task_id', 'target_sha', 'wheel', 'sha256')):
                raise ValueError('installation no longer matches the current built wheel')
        if 'tests.json' in documents:
            document = documents['tests.json']
            junit = evidence_path(root, tool + '/tests.xml')
            if (junit.stat().st_size > 20 * 1024 * 1024 or document.get('tool') != tool
                    or document.get('junit_sha256') != digest(junit)):
                raise ValueError('test summary has no matching bounded JUnit artifact')
            try:
                cases = list(ET.fromstring(junit.read_bytes()).iter('testcase'))
            except ET.ParseError as exc:
                raise ValueError('test JUnit artifact is malformed') from exc
            counts = {'tests': len(cases), 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
            for case in cases:
                outcome = next((key for key, tag in (('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped'))
                                if case.find(tag) is not None), 'passed')
                counts[outcome] += 1
            if (any(type(document.get(key)) is not int or document[key] != count for key, count in counts.items())
                    or not counts['passed'] or counts['failures'] or counts['errors']):
                raise ValueError('tests did not produce a nonempty passing JUnit selection')
            selected = document.get('selected_paths')
            if not isinstance(selected, list) or not selected or not all(isinstance(path, str) and path for path in selected):
                raise ValueError('test summary has no concrete test selection')
        if tool == 'environment' and documents['environment.json'].get('missing'):
            raise ValueError('environment reported missing prerequisites')
        if tool == 'flaggems':
            summary = documents['flaggems-summary.json'].get('summary', {})
            if not summary.get('total', 0) > 0 or summary.get('passed') != summary['total']:
                raise ValueError('FlagGems did not complete a nonempty passing test selection')

    def invoke(self, tool, parameters):
        if tool == 'status':
            if not self.lock.acquire(blocking=False):
                # Build/test calls keep their serialization lock. Status must
                # remain available while the agent reviews source alongside them.
                return {'status': 'running', 'policy': self.policy,
                        'active_command': self.active_command, 'cancelled': self.cancelled(),
                        'message': 'Wait for the original tool call; do not repeat the active check.'}
            try:
                return {'status': 'ready', 'policy': self.policy, 'checks': dict(self.checks),
                        'receipts': list(self.receipts), 'cancelled': self.cancelled()}
            finally:
                self.lock.release()
        with self.lock:
            if tool == 'log':
                receipt = next((r for r in self.receipts if r['id'] == parameters.get('receipt_id')), None)
                if not receipt:
                    raise ValueError('unknown receipt ID')
                path = self.output / receipt['log_path']
                if digest(path) != receipt['log_sha256']:
                    raise ValueError('log digest changed')
                return {'status': 'ready', 'receipt_id': receipt['id'],
                        'text': path.read_text(encoding='utf-8', errors='replace')[-24000:]}
            if tool == 'finalize':
                if self.closed or self.cancelled():
                    raise ValueError('task no longer accepts a review')
                if not isinstance(parameters.get('review'), dict):
                    raise ValueError('finalize needs review object')
                review = parameters['review']
                if not isinstance(review.get('summary'), str) or not review['summary'].strip():
                    raise ValueError('review.summary must be a nonempty string')
                statuses = {'architecture': {'passed', 'failed'}, 'pr_information': {'passed', 'failed'}}
                if self.context.get('event_kind') == 'push':
                    statuses['pr_information'].add('not_applicable')
                for name, allowed in statuses.items():
                    section = review.get(name)
                    if not isinstance(section, dict):
                        raise ValueError(f'review.{name} must be an object')
                    if not isinstance(section.get('status'), str) or section['status'] not in allowed:
                        raise ValueError(f'review.{name}.status must be one of: ' + ', '.join(sorted(allowed)))
                    if not isinstance(section.get('summary'), str) or not section['summary'].strip():
                        raise ValueError(f'review.{name}.summary must be a nonempty string')
                evidence = review['architecture'].get('evidence', [])
                if review['architecture']['status'] == 'passed':
                    validate_architecture_evidence(evidence, self.context.get('source_index'))
                elif not isinstance(evidence, list):
                    raise ValueError('review.architecture.evidence must be a list; an incomplete failed review may use []')
                for name in ('findings', 'uncompleted'):
                    if name in review and not isinstance(review[name], list):
                        raise ValueError(f'review.{name} must be a list')
                self.review = review
                write_json(self.output / 'agent-review.json', self.review)
                self.closed = True
                return {'status': 'submitted', 'message': 'Host will validate review and minimum checks.'}
            if self.closed or self.cancelled():
                raise ValueError('task no longer accepts commands')
            if tool not in (*TOOLS, 'control_plane', 'custom_test'):
                raise ValueError('unregistered tool')
            for dependency in ([] if tool in self.policy['not_applicable'] else DEPENDENCIES.get(tool, [])):
                if self.checks.get(dependency, {}).get('status') != 'passed':
                    raise ValueError(f'{tool} requires successful {dependency}')
            for name, fingerprints in self.artifact_fingerprints.items():
                if name in self.checks and self.checks[name]['status'] == 'passed':
                    for path, expected in fingerprints.items():
                        if not Path(path).is_file() or Path(path).is_symlink() or digest(path) != expected:
                            self.checks[name]['status'] = 'error'
                            raise ValueError(f'trusted {name} artifact changed; rebuild before continuing')
            invalidated = {tool}
            while True:
                following = {name for name, deps in DEPENDENCIES.items() if set(deps) & invalidated}
                if following <= invalidated:
                    break
                invalidated |= following
            for name in invalidated - {tool}:
                self.checks.pop(name, None)
            completed = [t for t, result in self.checks.items() if result['status'] == 'passed']
            context = {**self.context, 'profile': self.profile, 'completed_tools': completed}
            if tool == 'control_plane':
                from .control_plane import plan as control_plan
                plan = control_plan(context)
            elif tool == 'custom_test':
                path = parameters.get('path', '')
                if (not isinstance(path, str) or not path.endswith('.py') or '\\' in path
                        or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts):
                    raise ValueError('custom script must be a relative .py under artifacts/custom')
                args = parameters.get('args', [])
                if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
                    raise ValueError('custom args must be string argv')
                timeout = int(parameters.get('timeout', 300))
                if not 1 <= timeout <= 900:
                    raise ValueError('custom timeout must be between 1 and 900 seconds')
                plan = {'status': 'ready', 'commands': [{
                    'argv': [context.get('python_bin', 'python3'),
                             context['artifact_dir'] + '/custom/' + path, *args],
                    'cwd': context['source_dir'], 'env': {}, 'timeout': timeout}]}
            else:
                from tools.basic_tools.runner import plan as build_plan
                plan = build_plan(tool, context, parameters)
            evidence = []
            if plan['status'] == 'not_applicable':
                status, reason = 'not_applicable', plan.get('reason', 'profile lacks this capability')
            else:
                if not plan.get('commands'):
                    raise ValueError('tool has no executable commands')
                status, reason = 'passed', 'all commands completed successfully'
                for command in plan['commands']:
                    receipt = self.command(tool, command)
                    evidence.append(receipt['id'])
                    if receipt['termination'] or receipt['returncode']:
                        status = 'error' if receipt['termination'] else 'failed'
                        reason = receipt['termination'] or f"command exited {receipt['returncode']}"
                        break
            if status == 'passed':
                try:
                    self.verify_artifacts(tool)
                except (OSError, ValueError, KeyError) as exc:
                    status, reason = 'error', str(exc)
            result = {'id': tool, 'status': status, 'required': tool in self.policy['required'],
                      'reason': reason, 'evidence': evidence}
            self.checks[tool] = result
            artifact_host = self.context.get('artifact_host_dir')
            if status == 'passed' and artifact_host:
                directory = Path(artifact_host) / tool
                files = [*directory.glob('*.json'), *directory.glob('*.xml'), *directory.glob('wheels/*.whl')]
                self.artifact_fingerprints[tool] = {str(p): digest(p) for p in files if p.is_file() and not p.is_symlink()}
                write_json(self.output / 'artifact-fingerprints.json', self.artifact_fingerprints)
                if tool in ('compile_time', 'pass_profile', 'ir_serialization'):
                    self.performance = [x for x in self.performance if x['tool'] != tool]
                    self.performance.append({'tool': tool, 'comparison': read_json(directory / 'comparison.json'),
                                             'baseline': read_json(directory / 'baseline_identity.json')})
                    write_json(self.output / 'performance.json', self.performance)
            write_json(self.output / 'checks.json', list(self.checks.values()))
            return result

    def start(self, bind='0.0.0.0', port=0):
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                authorization = self.headers.get('Authorization', '')
                if not hmac.compare_digest(authorization, 'Bearer ' + broker.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if length < 1 or length > 256000:
                        raise ValueError('invalid request size')
                    request = json.loads(self.rfile.read(length))
                    result = broker.invoke(request['tool'], request.get('parameters', {}))
                    body, code = json.dumps(result, ensure_ascii=False).encode(), 200
                except Exception as exc:
                    body, code = json.dumps({'status': 'error', 'reason': str(exc)}).encode(), 400
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer((bind, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def stop(self):
        self.closed = True
        if self.active:
            self.active()
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
        with self.lock:
            pass
