"""Attest the actual host control tree before it may issue production CI evidence."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


SCOPES = ("scripts/local_ci", "scripts/dashboard", ".github")
MANIFEST_SCHEMA = "triton-anchor-local-ci-control"
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")
CACHES = {"__pycache__", ".pytest_cache"}


def _implementation_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ignored(path: Path) -> bool:
    return bool(set(path.parts) & CACHES) or path.suffix in {".pyc", ".pyo"}


def _files(root: Path) -> dict[str, str]:
    files = {}
    for scope in SCOPES:
        directory = root / scope
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing or symlinked trusted control directory: {scope}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root)
            if _ignored(relative):
                continue
            if path.is_symlink():
                raise ValueError(f"Control tree may not contain symlinks: {relative.as_posix()}")
            if path.is_file():
                files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("Trusted control tree contains no files")
    return files


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="strict", timeout=30)


def _identity(verified: bool, mode: str, revision: str | None, files: dict) -> dict:
    packed = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"verified": verified, "mode": mode, "actual_sha": revision,
            "files": files, "tree_sha256": hashlib.sha256(packed).hexdigest()}


def verify_control(config: dict, task: dict) -> dict:
    """Verify HEAD plus complete control hashes, or an explicitly trusted deployment manifest.

    ``local_acceptance: true`` permits an uncommitted development checkout but
    always returns ``verified: false``. Its result must never satisfy GitHub's
    production check. The caller records this identity in the resulting report.
    """
    actual_root = _implementation_root().resolve()
    root = Path(config.get("control_root", actual_root)).resolve()
    if root != actual_root:
        raise ValueError("Configured control_root is not the running implementation root")
    files = _files(root)
    expected = task.get("worker_revision_sha", "")
    if not isinstance(expected, str) or not SHA.fullmatch(expected):
        raise ValueError("Task has no valid frozen worker revision")
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        top = None
    in_git = bool(top is not None and top.returncode == 0)
    revision = None
    if in_git:
        if Path(top.stdout.strip()).resolve() != root:
            raise ValueError("Control implementation is not at its Git repository root")
        head = _git(root, "rev-parse", "HEAD")
        if head.returncode:
            raise ValueError("Cannot read trusted control HEAD")
        revision = head.stdout.strip()
    if config.get("local_acceptance") is True:
        return _identity(False, "local_acceptance", revision, files)
    if in_git:
        if revision != expected:
            raise ValueError("Running control HEAD differs from frozen worker_revision_sha")
        changed = _git(root, "diff", "--quiet", "HEAD", "--", *SCOPES)
        if changed.returncode:
            raise ValueError("Trusted control files have uncommitted changes")
        tracked = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", *SCOPES)
        if tracked.returncode:
            raise ValueError("Cannot enumerate committed control files")
        committed = {}
        for entry in tracked.stdout.split("\0"):
            if not entry:
                continue
            header, name = entry.split("\t", 1)
            mode, kind, object_id = header.split(" ")
            if _ignored(Path(name)):
                continue
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError("Control tree contains a symlink or gitlink entry")
            committed[name] = object_id
        if set(committed) != set(files):
            raise ValueError("Control tree has missing or extra files outside its committed identity")
        for name, object_id in committed.items():
            content = (root / name).read_bytes()
            actual_blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            if actual_blob != object_id:
                raise ValueError("Actual control bytes differ from HEAD; index flags cannot hide changes")
        return _identity(True, "git", revision, files)
    # A copied deployment has no Git history. Only host-selected full manifests are accepted.
    manifest_path = config.get("control_manifest")
    if not manifest_path:
        raise ValueError("Deployment copy requires an explicitly trusted control_manifest")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA
            or manifest.get("revision") != expected):
        raise ValueError("Deployment control manifest does not match the frozen worker revision")
    recorded = manifest.get("files")
    if not isinstance(recorded, dict) or not recorded or any(
            not isinstance(path, str) or not isinstance(value, str) or not DIGEST.fullmatch(value)
            for path, value in recorded.items()):
        raise ValueError("Deployment control manifest has an invalid file ledger")
    if recorded != files:
        raise ValueError("Deployment control files differ from the complete trusted manifest")
    return _identity(True, "manifest", expected, files)


# Sent as a literal argument to system Python, independent of mounted CI code.
CONTAINER_PROBE = r'''
import hashlib, json, re
from pathlib import Path
root = Path('/opt/anchor-ci')
if not root.is_dir() or root.is_symlink() or root.resolve() != root:
    raise SystemExit('control mount is missing or symlinked')
mounts = []
for line in Path('/proc/self/mountinfo').read_text().splitlines():
    fields = line.split()
    mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
    path = Path(mount)
    if path == root or path in root.parents or root in path.parents:
        mounts.append((path, fields[5].split(',')))
covering = [(path, options) for path, options in mounts if path == root or path in root.parents]
if not covering or 'ro' not in max(covering, key=lambda item: len(item[0].parts))[1]:
    raise SystemExit('control mount is writable')
if any('ro' not in options for path, options in mounts if root in path.parents):
    raise SystemExit('control subtree contains a writable mount')
files = {}
for path in sorted(root.rglob('*')):
    relative = path.relative_to(root)
    if set(relative.parts) & {'__pycache__', '.pytest_cache'} or path.suffix in {'.pyc', '.pyo'}:
        continue
    if path.is_symlink():
        raise SystemExit('control tree contains a symlink')
    if path.is_file():
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({'files': files, 'mount_read_only': True}, sort_keys=True))
'''


def verify_container_control(config: dict, profile: dict, control_identity: dict, *, run=subprocess.run) -> dict:
    """Compare the actual read-only worker control mount with the host file ledger.

    This check also applies to local acceptance. It trusts neither the profile's
    requested mounts nor code imported from the container control directory.
    """
    name = profile['container']['name']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]+', name):
        raise ValueError('Invalid persistent worker name')
    docker = config.get('docker', 'docker')
    inspected = run([docker, 'inspect', '--type', 'container', name], capture_output=True,
                    text=True, encoding='utf-8', timeout=30)
    if inspected.returncode:
        raise ValueError('Cannot inspect the persistent worker control mount')
    records = json.loads(inspected.stdout)
    if not isinstance(records, list) or len(records) != 1 or not records[0].get('State', {}).get('Running'):
        raise ValueError('Persistent worker is not running')
    info = records[0]
    root = PurePosixPath('/opt/anchor-ci')
    mounts = info.get('Mounts', [])
    matching = [m for m in mounts if isinstance(m, dict) and m.get('Destination') == str(root)]
    if len(matching) != 1 or matching[0].get('RW') is not False or matching[0].get('Type') not in {'bind', 'volume'}:
        raise ValueError('Persistent worker requires an explicit read-only /opt/anchor-ci mount')
    for mount in mounts:
        path = PurePosixPath(mount.get('Destination', ''))
        if root in path.parents and mount.get('RW') is not False:
            raise ValueError('Persistent worker has a writable mount inside its control tree')
    observed = run([docker, 'exec', '--user', '0', name, '/usr/bin/python3', '-I', '-c', CONTAINER_PROBE],
                   capture_output=True, text=True, encoding='utf-8', timeout=60)
    if observed.returncode:
        raise ValueError('Persistent worker control bytes or actual mount permissions could not be verified')
    actual = json.loads(observed.stdout)
    prefix = 'scripts/local_ci/'
    expected = {path[len(prefix):]: value for path, value in control_identity.get('files', {}).items()
                if path.startswith(prefix)}
    if not expected or actual.get('mount_read_only') is not True or actual.get('files') != expected:
        raise ValueError('Persistent worker control files differ from the actual host control tree')
    packed = json.dumps(expected, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'verified': True, 'mount_read_only': True, 'container_id': info.get('Id'),
            'tree_sha256': hashlib.sha256(packed).hexdigest()}
