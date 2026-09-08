"""Minimum coverage is trusted policy; the agent chooses ordering and additions."""
from __future__ import annotations

import fnmatch
import re

TOOLS = ('environment', 'frontend_build', 'wheel_install', 'frontend_smoke',
         'backend_rebuild', 'backend_smoke', 'flaggems', 'compile_time',
         'pass_profile', 'ir_serialization')
BACKEND_TOOLS = TOOLS[4:]
SHA = re.compile(r'[0-9a-f]{40}')
IDENTITY_FIELDS = ('repository', 'task_id', 'task_ref', 'event_kind', 'pr_number',
                   'target_branch', 'tested_sha', 'base_sha', 'head_sha',
                   'worker_revision_sha')


def validate_task(task, repository='anteloper-c/triton-anchor'):
    if task.get('schema') != 'triton-anchor-local-ci-task-metadata/v2':
        raise ValueError('unsupported task schema')
    if task.get('repository') != repository or repository == 'RACE-org/triton-anchor':
        raise ValueError('task repository is not authorized')
    from .common import safe_id
    safe_id(task.get('task_id'))
    if not re.fullmatch(r'ci/(pr-[1-9][0-9]*|push|full)/[^\s~^:?*\[\\]+', task.get('task_ref', '')):
        raise ValueError('invalid task ref')
    for field in ('tested_sha', 'base_sha', 'head_sha', 'worker_revision_sha'):
        if not SHA.fullmatch(task.get(field, '')):
            raise ValueError(f'invalid {field}')
    if task.get('target_sha') != task['tested_sha']:
        raise ValueError('target SHA differs from tested SHA')
    if task.get('execution_mode') == 'codex_only':
        raise ValueError('codex_only cannot bypass minimum coverage')
    if task.get('event_kind') == 'pull_request':
        if not isinstance(task.get('pr_number'), int) or isinstance(task['pr_number'], bool) or task['pr_number'] < 1:
            raise ValueError('invalid PR number')
        if len(task.get('title', '').strip()) < 5 or len(task.get('description', '').strip()) < 20:
            raise ValueError('PR title/description do not explain the change')
        if any(task.get('preflight', {}).get(k) != 'success' for k in ('pr_information', 'basic', 'api', 'security')):
            raise ValueError('PR preflight has not passed')
        approval = task.get('approval', {})
        external = task.get('head_repo') != repository
        if external and (approval.get('required') is not True or approval.get('status') != 'approved'):
            raise ValueError('external PR lacks approval')
        if external and any(approval.get(k) != task[k] for k in ('head_sha', 'base_sha', 'tested_sha', 'worker_revision_sha')):
            raise ValueError('approval identity is stale')
    return task


def minimum_checks(changed_paths, profile, manual_full=False):
    """Conservative floor. Documentation allowlist excludes executable config."""
    paths = list(changed_paths)
    supported = list(TOOLS if profile['triton_version'] == '3.0' else TOOLS[:4])
    docs = bool(paths) and all(
        (p.startswith('docs/') and p.lower().endswith(('.md', '.rst', '.txt', '.png', '.svg', '.jpg')))
        or ('/' not in p and p.lower().endswith(('.md', '.rst')))
        for p in paths)
    control = any(p.startswith(('.github/', 'scripts/local_ci/', 'scripts/dashboard/', 'dashboard/')) for p in paths)
    known = ('python/', 'csrc/', 'include/', 'tests/', 'docs/', 'scripts/', '.github/', 'dashboard/')
    packaging = ('setup.py', 'pyproject.toml', 'MANIFEST.in', 'CMakeLists.txt', 'envsetup.sh')
    unknown = not paths or any(not p.startswith(known) and p not in packaging for p in paths)
    compiler = any(p.startswith(('python/', 'csrc/', 'include/', 'tests/')) or p in packaging for p in paths)
    control_only = bool(paths) and all(p.startswith(('.github/', 'scripts/local_ci/', 'scripts/dashboard/', 'dashboard/')) for p in paths)
    required = [] if docs else ['environment'] if control_only else list(TOOLS[:4])
    reasons = {t: 'minimum frontend coverage' for t in required}
    deep_compiler = any(p.startswith(('csrc/', 'include/', 'triton/')) or
                        p in ('CMakeLists.txt', '.gitmodules', 'envsetup.sh') or
                        any(part in p.lower() for part in ('lowering', 'pipeline', 'adapter', 'hwcapability', 'jit', 'cache')) for p in paths)
    if not docs and (unknown or deep_compiler or manual_full):
        required = supported[:]
        reasons.update({t: 'compiler impact or unknown scope' for t in required})
    if control:
        required.append('control_plane')
        reasons['control_plane'] = 'CI control-plane behavior changed'
    if manual_full and 'flaggems' in supported:
        if 'flaggems' not in required:
            required.append('flaggems')
        reasons['flaggems'] = 'explicit manual full FlagGems request'
    required.append('architecture_review')
    reasons['architecture_review'] = 'architecture contract review is mandatory'
    return {'required': required, 'reasons': reasons, 'docs_only': docs,
            'supported': supported, 'not_applicable': [t for t in TOOLS if t not in supported],
            'changed_paths': paths, 'manual_full': manual_full}


def identity(task):
    return {key: task[key] for key in IDENTITY_FIELDS}
