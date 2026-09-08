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
from pathlib import Path

from .common import digest, execute, read_json, utcnow, write_json
from .policy import TOOLS

DEPENDENCIES = {
    'frontend_build': ['environment'], 'wheel_install': ['frontend_build'],
    'frontend_smoke': ['wheel_install'], 'backend_rebuild': ['wheel_install'],
    'backend_smoke': ['backend_rebuild'], 'flaggems': ['backend_smoke'],
    'compile_time': ['backend_smoke'], 'pass_profile': ['backend_smoke'],
    'ir_serialization': ['backend_smoke'],
}


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
                  self.profile['container']['name'], '/usr/bin/python3', '-I', '/opt/anchor-ci/runtime/container_process.py']
        argv = prefix + ['run', encoded]
        def stop():
            subprocess.run(prefix + ['stop', encoded], capture_output=True, timeout=15)
        self.active = stop
        log = self.output / 'logs' / (receipt_id + '.log')
        started = utcnow()
        write_json(self.output / 'active-command.json', {'id': receipt_id, 'tool': tool, 'started_at': started})
        result = execute(argv, log, timeout=min(command.get('timeout', 900), self.max_seconds-used),
                         cancelled=self.cancelled, terminate=stop)
        self.active = None
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
            'wheel_install': ['installation.json'], 'frontend_smoke': ['smoke_success.json'],
            'backend_rebuild': ['wheel.json', 'installation.json'], 'backend_smoke': ['smoke_success.json'],
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
        if tool in ('environment', 'frontend_build', 'wheel_install', 'frontend_smoke', 'backend_rebuild', 'backend_smoke'):
            for document in documents.values():
                if any(document.get(key) != self.context[key] for key in ('task_id', 'target_sha')):
                    raise ValueError('tool artifact identity differs from current task')
        if 'wheel.json' in documents:
            manifest = documents['wheel.json']
            prefix = self.context['artifact_dir'].rstrip('/') + '/'
            wheel = manifest.get('wheel', '')
            if not wheel.startswith(prefix + tool + '/wheels/'):
                raise ValueError('wheel manifest path is outside this build')
            actual = evidence_path(root, wheel[len(prefix):])
            if not actual.is_file() or digest(actual) != manifest.get('sha256'):
                raise ValueError('wheel manifest has no matching actual artifact')
        if tool == 'environment' and documents['environment.json'].get('missing'):
            raise ValueError('environment reported missing prerequisites')
        if tool == 'flaggems':
            summary = documents['flaggems-summary.json'].get('summary', {})
            if not summary.get('total', 0) > 0 or summary.get('passed') != summary['total']:
                raise ValueError('FlagGems did not complete a nonempty passing test selection')

    def invoke(self, tool, parameters):
        with self.lock:
            if tool == 'status':
                return {'status': 'ready', 'policy': self.policy, 'checks': self.checks,
                        'receipts': self.receipts, 'cancelled': self.cancelled()}
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
                evidence = review['architecture'].get('evidence')
                if not isinstance(evidence, list) or not evidence:
                    raise ValueError('review.architecture.evidence must be a nonempty list')
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
                plan = {'status': 'ready', 'commands': [{
                    'argv': [self.context['python_bin'], '-m', 'pytest', '-q', 'scripts/local_ci/tests', 'scripts/ci/tests'],
                    'cwd': self.context['source_dir'], 'env': {}, 'timeout': 900}]}
            elif tool == 'custom_test':
                path = parameters.get('path', '')
                if not isinstance(path, str) or not path.endswith('.py') or path.startswith('/') or '..' in Path(path).parts:
                    raise ValueError('custom test must be a relative .py under artifacts/custom')
                args = parameters.get('args', [])
                if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
                    raise ValueError('custom args must be string argv')
                plan = {'status': 'ready', 'commands': [{
                    'argv': [self.context.get('python_bin', 'python3'),
                             self.context['artifact_dir'] + '/custom/' + path, *args],
                    'cwd': self.context['source_dir'], 'env': {}, 'timeout': min(int(parameters.get('timeout', 300)), 900)}]}
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
                files = [*directory.glob('*.json'), *directory.glob('wheels/*.whl')]
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
