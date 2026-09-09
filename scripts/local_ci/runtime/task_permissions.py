"""Native task traversal and private host-to-agent document permissions."""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import stat
import sys
import tempfile


def prepare_ancestors(workspace: Path, task: Path) -> None:
    """Keep task siblings unlistable and unwritable to both untrusted UIDs."""
    if os.geteuid() != 0:
        raise PermissionError('Task ancestor preparation requires the trusted root worker process')
    workspace, task = Path(workspace), Path(task)
    if not workspace.is_absolute() or not task.is_absolute() or '..' in task.parts:
        raise ValueError('Task and workspace must be absolute canonical paths')
    relative = task.relative_to(workspace)
    if (len(relative.parts) != 3 or relative.parts[0] != 'tasks'
            or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', part) for part in relative.parts[1:])):
        raise ValueError('Task must be workspace/tasks/task_id/run_id')
    ancestors = [workspace, workspace / 'tasks', task.parent]
    # Validate the entire chain before changing any permissions. Never follow a
    # task-created link into host state or an unrelated mounted directory.
    for path in [*ancestors, task]:
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode) or path.resolve() != path:
            raise ValueError('Task ancestors must be real directories without symlinks')
    for path in ancestors:
        os.chown(path, 0, 0, follow_symlinks=False)
        os.chmod(path, 0o711, follow_symlinks=False)


def write_agent_document(path: Path, value: dict) -> None:
    """Root writes readable context within the agent's private 0700 directory."""
    path = Path(path)
    if (path.name not in {'context.json', 'publication-diagnostic.json'}
            or path.parent.name != 'agent' or path.parent.resolve() != path.parent):
        raise ValueError('Host document must remain in the real private agent directory')
    # The agent owns this directory, so it can pre-create predictable names.
    # An exclusive random temporary file followed by replace never follows an
    # existing target symlink. The private parent still excludes test UID 1000.
    descriptor, temporary = tempfile.mkstemp(prefix='.host-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), 0o644)
            else:
                # Windows keeps this file open here; never follow a replacement
                # symlink on runtimes whose chmod only accepts a pathname.
                os.chmod(temporary, 0o644, follow_symlinks=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: task_permissions.py /workspace/tasks/TASK/RUN')
    prepare_ancestors(Path('/workspace'), Path(sys.argv[1]))
