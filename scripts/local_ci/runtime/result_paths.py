"""Canonical run locations derived from task identity, shared by writers/readers."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value):
        raise ValueError('invalid task/run identifier')
    return value


def branch_directory(branch):
    """Keep ordinary branch names readable and encode slashes as one directory."""
    if (not isinstance(branch, str) or not branch or len(branch) > 200
            or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', branch)
            or '..' in branch or '@{' in branch
            or any(not part or part.startswith('.') or part.endswith(('.', '.lock')) for part in branch.split('/'))):
        raise ValueError('target branch cannot form a safe result directory')
    # Escape uppercase too so distinct Git branches never alias on Windows.
    encoded = ''.join(chr(byte) if byte in b'abcdefghijklmnopqrstuvwxyz0123456789-_.'
                      else f'%{byte:02X}' for byte in branch.encode('utf-8'))
    # Windows treats these names as devices, even with a suffix.
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', encoded, re.I):
        encoded = '%' + format(ord(encoded[0]), '02X') + encoded[1:]
    if len(encoded) > 240:
        raise ValueError('encoded target branch is too long')
    return encoded


def run_relative(identity, run_id):
    """Return the sole location for a new run; full tests remain push-mode runs."""
    task_id, run_id = _identifier(identity.get('task_id')), _identifier(run_id)
    branch = branch_directory(identity.get('target_branch'))
    number, kind, task_ref = identity.get('pr_number'), identity.get('event_kind'), identity.get('task_ref')
    if kind == 'pull_request':
        if (type(number) is not int or number < 1 or not isinstance(task_ref, str)
                or not task_ref.startswith(f'ci/pr-{number}/') or not task_ref[len(f'ci/pr-{number}/'):]):
            raise ValueError('PR result directory disagrees with task identity')
        branch_directory(task_ref[len(f'ci/pr-{number}/'):])
        group = f'pr/{branch}/pr-{number}'
    elif kind == 'push':
        if (type(number) is not int or number != 0 or not isinstance(task_ref, str)
                or not re.fullmatch(r'ci/(push|full)/.+', task_ref)):
            raise ValueError('push result directory disagrees with task identity')
        source_branch = task_ref.split('/', 2)[2]
        if source_branch != identity['target_branch']:
            raise ValueError('push task ref differs from target branch')
        branch_directory(source_branch)
        group = f'push/{branch}'
    else:
        raise ValueError('unsupported result event kind')
    return f'runs/{group}/{task_id}/{run_id}'


def legacy_run_relative(identity, run_id):
    return f"runs/{_identifier(identity.get('task_id'))}/{_identifier(run_id)}"


def validate_result_path(path, identity, run_id, filename='result.json', allow_legacy=True):
    if (not isinstance(path, str) or not path or '\\' in path or ':' in path
            or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
            or PurePosixPath(path).as_posix() != path):
        raise ValueError('result path must be a canonical relative path')
    if not re.fullmatch(r'[A-Za-z0-9_-]+[.]json', filename):
        raise ValueError('unsupported result filename')
    expected = run_relative(identity, run_id) + '/' + filename
    legacy = legacy_run_relative(identity, run_id) + '/' + filename
    if path != expected and not (allow_legacy and path == legacy):
        raise ValueError('result path differs from trusted task identity')
    return path


def iter_run_files(root, filename):
    """Read both known layouts, without recursively scanning task artifacts."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+[.]json', filename):
        raise ValueError('unsupported run filename')
    root = Path(root)
    patterns = (f'runs/*/*/{filename}', f'runs/pr/*/pr-*/*/*/{filename}',
                f'runs/push/*/*/*/{filename}')
    return sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})


def task_run_files(root, identity, filename='execution.json'):
    """Find an admitted task's existing grouped or historical run journals."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+[.]json', filename):
        raise ValueError('unsupported run filename')
    new_parent = PurePosixPath(run_relative(identity, 'run-placeholder')).parent
    old_parent = PurePosixPath(legacy_run_relative(identity, 'run-placeholder')).parent
    root = Path(root)
    return sorted({path for parent in (new_parent, old_parent)
                   for path in (root / parent).glob('*/' + filename) if path.is_file()})
