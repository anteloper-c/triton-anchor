"""Trusted syntax and routing checks for a candidate main tree; never execute it."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess


KIND = 'triton-anchor-ci-gateway'
EXPRESSIONS = re.compile(r'\$\{\{.*?\}\}', re.S)


def plan(context):
    """Bind the router exception to the host's target branch and candidate manifest."""
    source = Path(context['source_host_dir'])
    manifest = source / '.github/ci-gateway-manifest.json'
    router = False
    if context.get('target_branch') == 'main' and not (source / 'scripts').exists():
        if manifest.is_file() and not manifest.is_symlink():
            document = json.loads(read_file(source, manifest))
            router = document.get('kind') == KIND and document.get('role') == 'router'
    argv = (['/opt/ci-venv/bin/python', '-I',
             '/opt/anchor-ci/runtime/control_plane.py', '--source', context['source_dir']]
            if router else [context['python_bin'], '-m', 'pytest', '-q',
                            'scripts/local_ci/tests', 'scripts/ci/tests'])
    return {'status': 'ready', 'commands': [
        {'argv': argv, 'cwd': context['source_dir'], 'env': {}, 'timeout': 900}]}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_file(root, path):
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            f'not a regular candidate file: {path.relative_to(root)}')
    require(path.stat().st_size <= 1024 * 1024, 'candidate control file exceeds 1 MiB')
    return path.read_text(encoding='utf-8')


def syntax(script, shell, label):
    """Parse shell/JavaScript without executing candidate statements."""
    require(isinstance(script, str) and script.strip(), f'{label}: missing script')
    script = EXPRESSIONS.sub('ci_expression', script)
    if shell == 'python':
        ast.parse(script, filename=label)
        return
    if shell == 'javascript':
        argv, script = ['node', '--check'], '(async function () {\n' + script + '\n});\n'
    elif shell in ('bash', 'sh'):
        argv = [shell, '-n']
    else:
        raise ValueError(f'{label}: unsupported syntax checker for shell {shell}')
    checked = subprocess.run(argv, input=script, text=True, capture_output=True, timeout=30)
    require(checked.returncode == 0, f'{label}: {shell} syntax failed: {checked.stderr[:2000]}')


def load_workflow(text):
    # BaseLoader keeps GitHub's YAML "on" key as text and never constructs objects.
    import yaml
    class Loader(yaml.BaseLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                require(isinstance(key, str) and key not in result, 'duplicate or non-string YAML key')
                result[key] = self.construct_object(value_node, deep=deep)
            return result
    require(not any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)),
            'YAML aliases are not supported by the trusted router checker')
    value = yaml.load(text, Loader=Loader)
    require(isinstance(value, dict) and value.get('on'), 'workflow needs an event mapping/list/name')
    return value


def check_workflow(name, workflow):
    jobs = workflow.get('jobs')
    require(isinstance(jobs, dict) and jobs, f'{name}: workflow needs jobs')
    graph, scripts = {}, 0
    for job_id, job in jobs.items():
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', job_id) and isinstance(job, dict),
                f'{name}: invalid job')
        needs = job.get('needs', [])
        needs = [needs] if isinstance(needs, str) else needs
        require(isinstance(needs, list) and all(isinstance(n, str) and n in jobs for n in needs),
                f'{name}/{job_id}: unknown job dependency')
        graph[job_id] = needs
        if 'uses' in job:
            require(isinstance(job['uses'], str) and job['uses'] and 'steps' not in job,
                    f'{name}/{job_id}: invalid reusable workflow job')
            continue
        steps = job.get('steps')
        require(job.get('runs-on') and isinstance(steps, list) and steps,
                f'{name}/{job_id}: job needs a runner and steps')
        for index, step in enumerate(steps):
            label = f'{name}/{job_id}/step-{index + 1}'
            require(isinstance(step, dict) and (('run' in step) != ('uses' in step)),
                    f'{label}: needs exactly one of run or uses')
            if 'run' in step:
                default = job.get('defaults', workflow.get('defaults', {})).get('run', {}).get('shell', 'bash')
                syntax(step['run'], step.get('shell', default), label)
                scripts += 1
            else:
                require(isinstance(step['uses'], str) and step['uses'], f'{label}: invalid action')
                if step['uses'].startswith('actions/github-script@'):
                    syntax(step.get('with', {}).get('script'), 'javascript', label)
                    scripts += 1
    visited, active = set(), set()
    def visit(job_id):
        require(job_id not in active, f'{name}: cyclic job dependencies')
        if job_id not in visited:
            active.add(job_id)
            for dependency in graph[job_id]:
                visit(dependency)
            active.remove(job_id)
            visited.add(job_id)
    for job_id in graph:
        visit(job_id)
    return scripts


def check_router(root):
    root = Path(root).resolve()
    require(not (root / 'scripts').exists(), 'router exception cannot replace worker tests')
    manifest_path = root / '.github/ci-gateway-manifest.json'
    manifest_text = read_file(root, manifest_path)
    manifest = json.loads(manifest_text)
    require(manifest.get('kind') == KIND and manifest.get('role') == 'router',
            'candidate must explicitly identify the trusted router contract')
    workflow_dir = root / '.github/workflows'
    paths = sorted([*workflow_dir.glob('*.yml'), *workflow_dir.glob('*.yaml')])
    require(1 <= len(paths) <= 50, 'router needs a bounded nonempty workflow set')
    workflows, evidence, count = {}, {}, 0
    for path in paths:
        text = read_file(root, path)
        workflow = load_workflow(text)
        count += check_workflow(path.name, workflow)
        workflows[path.name] = workflow
        evidence[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    gateway = workflows.get('ci-gateway.yml', {})
    require(gateway.get('env', {}).get('GATEWAY_KIND') == KIND, 'gateway kind is missing or incompatible')
    events = gateway.get('on', {})
    require(isinstance(events, dict) and 'pull_request_target' in events and 'workflow_dispatch' in events,
            'gateway must route PR and manual events')
    inputs = (events.get('workflow_dispatch') or {}).get('inputs', {})
    require({'task_id', 'expected_head_sha', 'comparison_base_sha', 'tested_sha', 'worker_revision_sha'} <= set(inputs),
            'gateway is missing immutable identity inputs')
    require({'route-cancellation', 'prepare-route', 'route-pull-request', 'route-manual-push',
             'route-failure-status'} <= set(gateway.get('jobs', {})), 'gateway is missing a routing path')
    watchdog = workflows.get('local-ci-watchdog.yml', {})
    require('schedule' in watchdog.get('on', {}) and 'workflow_dispatch' in watchdog.get('on', {}),
            'watchdog router needs scheduled and manual triggers')
    forward = watchdog.get('jobs', {}).get('forward', {})
    require(forward.get('uses') == 'anteloper-c/triton-anchor/.github/workflows/local-ci-watchdog.yml@ci_repo',
            'watchdog must delegate to the maintained ci_repo implementation')
    for path in sorted((root / '.github/scripts').rglob('*.py')):
        ast.parse(read_file(root, path), filename=path.relative_to(root).as_posix())
        evidence[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        count += 1
    evidence[manifest_path.relative_to(root).as_posix()] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {'status': 'passed', 'validation': 'candidate-router-syntax-and-contracts',
            'workflow_count': len(workflows), 'script_count': count, 'sha256': evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(check_router(args.source), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
