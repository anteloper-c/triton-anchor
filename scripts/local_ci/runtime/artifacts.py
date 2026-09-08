"""Freeze bounded task evidence after all worker processes have stopped."""
from pathlib import Path
import shutil

from .common import digest, write_json

SUFFIXES = {'.py', '.md', '.txt', '.json', '.jsonl', '.csv', '.tsv', '.mlir', '.ll', '.log',
            '.ttir', '.ttgir', '.llir', '.ptx', '.s'}


def collect_artifacts(source, output, *, max_file_bytes=8*1024*1024,
                      max_total_bytes=32*1024*1024, max_files=512):
    """Copy evidence, not environments, wheels, credentials, or source checkouts.

    This runs on the host after cleanup. A manifest records every omission so a
    large or unsupported artifact is never silently represented as published.
    """
    source, output = Path(source), Path(output)
    files, omitted, total = [], [], 0
    if source.exists():
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_dir() and not path.is_symlink():
                continue
            reason = None
            if path.is_symlink() or any((source / part).is_symlink() for part in relative.parents):
                reason = 'symlink is not publishable'
            elif not path.is_file() or path.suffix.lower() not in SUFFIXES:
                reason = 'not a supported text evidence artifact'
            elif any(part.startswith('.') for part in relative.parts):
                reason = 'hidden file or directory'
            elif path.stat().st_size > max_file_bytes:
                reason = 'per-file publication limit'
            elif len(files) >= max_files or total + path.stat().st_size > max_total_bytes:
                reason = 'total publication limit'
            if reason:
                omitted.append({'path': relative.as_posix(), 'reason': reason})
                continue
            destination = output / 'artifacts' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            size = destination.stat().st_size
            total += size
            files.append({'path': destination.relative_to(output).as_posix(),
                          'sha256': digest(destination), 'size': size})
    manifest = {'schema': 'triton-anchor-local-ci-artifacts/v1', 'files': files,
                'omitted': omitted, 'total_bytes': total}
    write_json(output / 'artifact-manifest.json', manifest)
    return manifest
