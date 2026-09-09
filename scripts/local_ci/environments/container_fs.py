#!/usr/bin/env python3
"""Fixed management operations inside one rootless PR container.

Never accepts host paths, arbitrary UIDs or arbitrary management commands.
Secrets arrive only on stdin; export roots exclude the Codex volume.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

TASK = Path("/task")
CODEX = Path("/codex")
CONTROL = TASK / ".control"
MAX_ARCHIVE = 4 * 1024**3
MAX_EXPORT = 512 * 1024**2
ROLES = ("candidate", "base", "diagnostic", "codex")


def safe_name(value, pattern=r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid bounded object name")
    return value


def checked(root: Path, relative: str, *, exists=False):
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"..", "."} for part in path.parts)
    ):
        raise ValueError("Only relative paths within a fixed task scope are allowed")
    target = root / path
    for part in (
        root,
        *[root.joinpath(*path.parts[:i]) for i in range(1, len(path.parts) + 1)],
    ):
        if part.is_symlink():
            raise ValueError("Symlinked management paths are forbidden")
    if exists and not target.exists():
        raise ValueError("Task object is missing")
    return target


def manifest():
    path = CONTROL / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != 0:
        raise ValueError("Trusted task manifest is unavailable")
    return json.loads(path.read_text())


def own(path: Path, uid: int, gid: int, mode: int):
    if path.is_symlink():
        raise ValueError("Refusing ownership changes through a symlink")
    os.chown(path, uid, gid)
    path.chmod(mode)


def write(path: Path, payload: bytes, *, uid=0, gid=0, mode=0o600):
    if path.is_symlink():
        raise ValueError("Refusing symlinked output")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".management-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        own(Path(temp), uid, gid, mode)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def walk(root, *, reject_links=False):
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            if path.is_symlink():
                if reject_links:
                    raise ValueError("Artifact symlinks are forbidden")
                continue
            info = path.lstat()
            if reject_links and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("Artifact hardlinks are forbidden")
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise ValueError("Special files are not accepted")
            if path.is_dir() and os.path.ismount(path):
                raise ValueError("Nested mounts are forbidden")
            yield path


def set_tree_identity(root, uid, gid):
    for path in [root, *walk(root)]:
        mode = path.stat().st_mode
        own(
            path, uid, gid, 0o750 if path.is_dir() else 0o750 if mode & 0o111 else 0o640
        )


def init_task(payload):
    if (CONTROL / "manifest.json").exists():
        previous = manifest()
        if previous != payload:
            raise ValueError("Task identity cannot change within a volume")
        return {"initialized": True}
    uids, gids = payload["uids"], payload["gids"]
    if (
        set(uids) != set(ROLES)
        or len(set(uids.values())) != 4
        or any(type(uid) is not int or uid <= 0 for uid in uids.values())
    ):
        raise ValueError("Four distinct non-root task identities are required")
    if any(type(gids.get(role)) is not int or gids[role] <= 0 for role in ROLES):
        raise ValueError("Non-root groups are required")
    if gids["codex"] in {gids[role] for role in ROLES if role != "codex"}:
        raise ValueError("Codex group must be private")
    for path in (TASK, CODEX):
        path.mkdir(parents=True, exist_ok=True)
        own(path, 0, 0, 0o711)
    CONTROL.mkdir(exist_ok=True)
    own(CONTROL, 0, 0, 0o700)
    for role in ("candidate", "base"):
        root = TASK / role
        root.mkdir(exist_ok=True)
        own(root, uids[role], gids[role], 0o750)
    for name in ("artifacts", "experiments", "diagnostics", ".trusted"):
        root = TASK / name
        root.mkdir(exist_ok=True)
        own(root, 0, 0, 0o711)
    for name, mode in (("home", 0o700), ("workspace", 0o750)):
        root = CODEX / name
        root.mkdir(exist_ok=True)
        own(root, uids["codex"], gids["codex"], mode)
    write(CONTROL / "manifest.json", json.dumps(payload, sort_keys=True).encode())
    return {"initialized": True}


def extract(source, destination, expected_digest):
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as temporary:
        total = 0
        while block := source.read(1024 * 1024):
            total += len(block)
            if total > MAX_ARCHIVE:
                raise ValueError("Source archive exceeds the task import limit")
            temporary.write(block)
            digest.update(block)
        if digest.hexdigest() != expected_digest:
            raise ValueError("Source archive checksum mismatch")
        temporary.seek(0)
        with tarfile.open(fileobj=temporary, mode="r:*") as archive:
            members = [
                m
                for m in archive.getmembers()
                if not (m.isdir() and m.name in {".", "./"})
            ]
            total = 0
            for member in members:
                checked(destination, member.name)
                if not member.isdir() and not member.isfile() and not member.issym():
                    raise ValueError(
                        "Source archives allow only directories, files and internal symlinks"
                    )
                total += member.size
                if total > MAX_ARCHIVE:
                    raise ValueError("Expanded source archive is too large")
                if member.issym():
                    target = Path(member.name).parent / member.linkname
                    if Path(member.linkname).is_absolute() or not (
                        destination / target
                    ).resolve().is_relative_to(destination.resolve()):
                        raise ValueError("Source archive symlink escapes its checkout")
            destination.mkdir()
            # Regular entries first, links last; no extraction operation follows a link.
            for member in [m for m in members if not m.issym()] + [
                m for m in members if m.issym()
            ]:
                target = checked(destination, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                elif member.issym():
                    target.symlink_to(member.linkname)
                else:
                    if target.exists():
                        raise ValueError("Duplicate archive file")
                    with (
                        archive.extractfile(member) as content,
                        target.open("xb") as output,
                    ):
                        shutil.copyfileobj(content, output)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)


def import_checkout(params, stream):
    data = manifest()
    variant = params["variant"]
    if variant not in {"candidate", "base"}:
        raise ValueError("Invalid checkout variant")
    marker = CONTROL / (variant + "-import.json")
    destination = checked(TASK, variant + "/checkout")
    identity = {key: params[key] for key in ("sha256", "expected_sha")}
    if marker.exists():
        if json.loads(marker.read_text()) != identity or not destination.is_dir():
            raise ValueError("Existing checkout import identity changed")
        return {"imported": False, "reused": True}
    if destination.exists():
        raise ValueError("Incomplete checkout import requires a new attempt")
    extract(stream, destination, params["sha256"])
    if git(destination, "rev-parse", "HEAD") != params["expected_sha"]:
        raise ValueError("Imported checkout differs from frozen commit")
    trusted = source_manifest(destination)
    write(
        CONTROL / (variant + "-source-manifest.json"),
        json.dumps(
            {"sha": params["expected_sha"], "files": trusted}, sort_keys=True
        ).encode(),
    )
    set_tree_identity(destination, data["uids"][variant], data["gids"][variant])
    backend = data.get("env", {}).get("BACKEND_PATH")
    if data.get("backend_enabled") and backend:
        target = checked(TASK, variant + "/backend")
        shutil.copytree(backend, target, symlinks=True)
        set_tree_identity(target, data["uids"][variant], data["gids"][variant])
    write(marker, json.dumps(identity).encode())
    return {"imported": True, "path": str(destination)}


def git(root, *arguments):
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "safe.directory=" + str(root),
        *arguments,
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(CONTROL),
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("Frozen checkout Git verification failed")
    return result.stdout.decode().strip()


def source_entry(root, name):
    path = root / name
    # The final symlink itself is an object to compare, never a target to read.
    if any(
        parent.is_symlink()
        for parent in [root, *path.parents]
        if parent.is_relative_to(root)
    ):
        raise ValueError("Tracked source parent became a symlink")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"type": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return {
            "type": "file",
            "sha256": digest.hexdigest(),
            "executable": bool(info.st_mode & 0o111),
        }
    if stat.S_ISDIR(info.st_mode):
        return {"type": "directory"}
    raise ValueError("Tracked source is not an ordinary Git object")


def source_manifest(root):
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "safe.directory=" + str(root),
        "cat-file",
        "--batch",
    ]
    process = subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(CONTROL),
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    try:
        return _source_manifest(root, process)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def _source_manifest(root, process):
    entries = {}
    for record in git(root, "ls-tree", "-rz", "--full-tree", "HEAD").split("\x00"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, blob = metadata.split()
        checked(root, str(Path(name).parent)) if Path(name).parent != Path(
            "."
        ) else None
        entry = source_entry(root, name)
        if kind == "blob":
            process.stdin.write((blob + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline(256).decode().split()
            if len(header) != 3 or header[:2] != [blob, "blob"]:
                raise ValueError("Git object stream identity differs")
            remaining = int(header[2])
            if (
                remaining < 0
                or remaining > MAX_ARCHIVE
                or mode == "120000"
                and remaining > 65536
            ):
                raise ValueError("Tracked Git blob exceeds its bound")
            digest, raw = hashlib.sha256(), bytearray()
            while remaining:
                block = process.stdout.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("Git object stream ended early")
                remaining -= len(block)
                digest.update(block)
                if mode == "120000":
                    raw.extend(block)
            if process.stdout.read(1) != b"\n":
                raise ValueError("Invalid Git object stream delimiter")
            if mode == "120000":
                if entry != {"type": "symlink", "target": raw.decode()}:
                    raise ValueError("Frozen source symlink differs from commit")
            elif entry != {
                "type": "file",
                "sha256": digest.hexdigest(),
                "executable": mode == "100755",
            }:
                raise ValueError("Frozen source content differs from commit")
        elif mode == "160000":
            # Submodules, when materialized by the trusted relay, are frozen
            # recursively; empty gitlink directories remain an explicit entry.
            if (root / name / ".git").exists():
                if git(root / name, "rev-parse", "HEAD") != blob:
                    raise ValueError("Frozen submodule revision differs from gitlink")
                for subpath, item in source_manifest(root / name).items():
                    entries[name + "/" + subpath] = item
        else:
            raise ValueError("Unexpected tracked Git object")
        entries[name] = entry
    return entries


def verify_checkout(params):
    variant = params["variant"]
    if variant not in {"candidate", "base"}:
        raise ValueError("Invalid checkout variant")
    root = checked(TASK, variant + "/checkout", exists=True)
    saved = json.loads((CONTROL / (variant + "-source-manifest.json")).read_text())
    try:
        dirty = any(
            source_entry(root, path) != entry for path, entry in saved["files"].items()
        )
    except (OSError, ValueError):
        dirty = True
    verified = saved["sha"] == params["expected_sha"] and not dirty
    return {
        "verified": bool(verified),
        "sha": saved["sha"],
        "dirty": bool(dirty),
        "clean": bool(verified),
        "head": saved["sha"],
    }


def prepare_execution(params):
    data = manifest()
    ident = safe_name(params["execution_id"], r"[a-f0-9]{32}")
    role = "diagnostic" if params.get("diagnostic") else params["variant"]
    if role not in {"candidate", "base", "diagnostic"}:
        raise ValueError("Invalid execution role")
    root = checked(TASK, "artifacts/" + ident)
    root.mkdir(exist_ok=True)
    own(root, data["uids"][role], data["gids"][role], 0o750)
    scripts = checked(TASK, ".trusted/scripts/" + ident)
    scripts.mkdir(parents=True, exist_ok=True)
    own(scripts.parent, 0, 0, 0o711)
    own(scripts, 0, data["gids"][role], 0o750)
    base = (
        checked(TASK, params["variant"])
        if role != "diagnostic"
        else checked(TASK, "diagnostics/" + ident)
    )
    base.mkdir(exist_ok=True)
    own(base, data["uids"][role], data["gids"][role], 0o750)
    for name in ("home", "tmp", "cache", "state"):
        path = checked(base, name)
        path.mkdir(exist_ok=True)
        own(path, data["uids"][role], data["gids"][role], 0o750)
    return {
        "artifact_dir": str(root),
        "script_dir": str(scripts),
        "home": str(base / "home"),
        "tmp": str(base / "tmp"),
    }


def write_execution_file(params, content):
    ident = safe_name(params["execution_id"], r"[a-f0-9]{32}")
    name = safe_name(params["name"])
    root = checked(TASK, ".trusted/scripts/" + ident, exists=True)
    if len(content) > 2 * 1024**2:
        raise ValueError("Execution script too large")
    write(root / name, content, gid=manifest()["gids"]["diagnostic"], mode=0o440)
    return {"path": str(root / name)}


def export_archive(root, output, *, exclude=()):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Export root is unavailable")
    selected, total = [], 0
    for path in walk(root, reject_links=True):
        if path.is_file() and path.name not in exclude:
            total += path.stat().st_size
            if total > MAX_EXPORT:
                raise ValueError("Evidence export exceeds its limit")
            selected.append(path)
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for path in sorted(selected):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Export file changed type")
                member = tarfile.TarInfo(path.relative_to(root).as_posix())
                member.size, member.mode = info.st_size, 0o600
                archive.addfile(member, source)


def require_idle(data):
    formal = {data["uids"]["candidate"], data["uids"]["base"]}
    for path in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(
                line.split(":", 1)
                for line in path.read_text().splitlines()
                if ":" in line
            )
            if not fields["State"].strip().startswith("Z") and formal.intersection(
                map(int, fields["Uid"].split())
            ):
                raise ValueError(
                    "Formal task processes must stop before diagnostic authorization"
                )
        except (FileNotFoundError, ProcessLookupError):
            continue


def authorize_diagnostics():
    data = manifest()
    require_idle(data)
    for role in ("candidate", "base"):
        for name in ("checkout", "venv", "backend"):
            root = checked(TASK, role + "/" + name)
            if root.is_dir():
                set_tree_identity(root, data["uids"][role], data["gids"]["diagnostic"])
    return {"authorized": True}


def create_experiment(params):
    data = manifest()
    require_idle(data)
    ident, variant = (
        safe_name(params["experiment_id"]),
        params.get("variant", "candidate"),
    )
    if variant not in {"candidate", "base"}:
        raise ValueError("Invalid experiment source")
    root = checked(TASK, "experiments/" + ident)
    marker = CONTROL / ("experiment-" + ident + ".json")
    if root.exists():
        if not marker.is_file() or json.loads(marker.read_text()) != {
            "variant": variant
        }:
            raise ValueError("Experiment identity differs or preparation is incomplete")
        return {
            "experiment_id": ident,
            "root": str(root),
            "checkout": str(root / "checkout"),
            "venv": str(root / "venv"),
        }
    root.mkdir()
    shutil.copytree(
        checked(TASK, variant + "/checkout", exists=True),
        root / "checkout",
        symlinks=True,
    )
    backend = checked(TASK, variant + "/backend")
    if backend.exists():
        shutil.copytree(backend, root / "backend", symlinks=True)
    for name in ("home", "tmp", "cache", "state"):
        (root / name).mkdir()
    seed_venv(root, data.get("env", {}))
    set_tree_identity(root, data["uids"]["diagnostic"], data["gids"]["diagnostic"])
    write(marker, json.dumps({"variant": variant}).encode())
    return {
        "experiment_id": ident,
        "root": str(root),
        "checkout": str(root / "checkout"),
        "venv": str(root / "venv"),
    }


def seed_venv(root, environment):
    """Copy packages only from the immutable image interpreter, never PR Python."""
    seed = environment.get("SEED_PYTHON") or str(
        Path(environment.get("PYTHON_VENV_ACTIVATE", "/opt/venv/bin/activate")).parent
        / "python"
    )
    subprocess.run(
        [seed, "-I", "-m", "venv", "--copies", str(root / "venv")],
        check=True,
        timeout=120,
    )
    # Ask the isolated trusted seed for its actual package layout, including
    # Debian dist-packages. No candidate-controlled Python is queried as root.
    query = "import json,sys;print(json.dumps({'version':str(sys.version_info.major)+'.'+str(sys.version_info.minor),'paths':[p for p in sys.path if p.endswith(('site-packages','dist-packages'))]}))"
    layout = json.loads(
        subprocess.run(
            [seed, "-I", "-c", query], check=True, timeout=30, capture_output=True
        ).stdout
    )
    if not re.fullmatch(r"[0-9]+\.[0-9]+", layout["version"]):
        raise ValueError("Invalid trusted seed Python layout")
    target = root / "venv/lib" / ("python" + layout["version"]) / "site-packages"
    # Earlier entries in sys.path take precedence, so copy them last.
    for value in reversed(layout["paths"]):
        source = Path(value)
        if not source.is_absolute() or not source.is_dir():
            continue
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=False)


def trusted_json(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise ValueError("Trusted native workspace metadata changed")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as stream:
        return json.load(stream)


def native_layout(root):
    return {
        "root": str(root),
        "checkout": str(root / "checkout"),
        "venv": str(root / "venv"),
        "python_bin": str(root / "venv/bin/python"),
        "home": str(root / "home"),
        "tmp": str(root / "tmp"),
        "cache": str(root / "cache"),
        "backend": str(root / "backend") if (root / "backend").is_dir() else None,
    }


def native_directory_identity(root, data):
    result = {}
    names = [".", "checkout", "venv", "home", "tmp", "cache", "state"]
    if data.get("backend_enabled"):
        names.append("backend")
    for name in names:
        path = root if name == "." else checked(root, name, exists=True)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != data["uids"]["codex"]
            or info.st_gid != data["gids"]["codex"]
            or info.st_mode & 0o027
        ):
            raise ValueError("Native workspace directory identity changed")
        result[name] = [info.st_dev, info.st_ino]
    return result


def prepare_native_workspace(params):
    """Make an editable exploratory checkout without changing formal evidence."""
    data = manifest()
    expected_sha = safe_name(params["expected_sha"], r"[a-f0-9]{40,64}")
    identity = {key: data[key] for key in ("task_id", "run_id", "attempt_id")} | {
        "source_sha": expected_sha
    }
    root = checked(CODEX, "workspace/candidate")
    marker = CONTROL / "native-workspace.json"
    if marker.exists() or marker.is_symlink():
        saved = trusted_json(marker)
        if saved.get("identity") != identity or saved.get(
            "directories"
        ) != native_directory_identity(root, data):
            raise ValueError("Native workspace identity changed")
        return {**native_layout(root), "source_sha": expected_sha, "reused": True}
    if root.exists():
        raise ValueError("Incomplete native workspace requires a new attempt")
    require_idle(data)
    source = checked(TASK, "candidate/checkout", exists=True)
    saved = trusted_json(CONTROL / "candidate-source-manifest.json")
    if not verify_checkout({"variant": "candidate", "expected_sha": expected_sha})[
        "verified"
    ]:
        raise ValueError("Native workspace source differs from the frozen candidate")
    root.mkdir(mode=0o700)
    checkout = root / "checkout"
    checkout.mkdir()
    # Only copy the root-owned initial source inventory. Never follow PR links,
    # copy untracked files, or execute mutable candidate Git/Python as root.
    for name, entry in saved["files"].items():
        target = checked(checkout, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["type"] == "directory":
            target.mkdir(exist_ok=True)
        elif entry["type"] == "symlink":
            target.symlink_to(entry["target"])
        else:
            origin = source / name
            descriptor = os.open(origin, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Native source must be an unlinked regular file")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
            target.chmod(0o750 if entry["executable"] else 0o640)
            if source_entry(checkout, name) != entry:
                raise ValueError("Native source changed during preparation")
    environment = data.get("env", {})
    if data.get("backend_enabled"):
        backend = Path(environment.get("BACKEND_PATH", ""))
        if not backend.is_absolute() or not backend.is_dir():
            raise ValueError("Trusted native backend source is unavailable")
        shutil.copytree(backend, root / "backend", symlinks=True)
    for name in ("home", "tmp", "cache", "state"):
        (root / name).mkdir()
    seed_venv(root, environment)
    set_tree_identity(root, data["uids"]["codex"], data["gids"]["codex"])
    write(
        marker,
        json.dumps(
            {
                "identity": identity,
                "directories": native_directory_identity(root, data),
                "source_files": saved["files"],
            },
            sort_keys=True,
        ).encode(),
    )
    return {**native_layout(root), "source_sha": expected_sha, "reused": False}


NATIVE_EXCLUDED = {
    ".git",
    "venv",
    ".venv",
    "backend",
    "home",
    "tmp",
    "cache",
    "state",
    "build",
    "dist",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    "auth.json",
    "config.toml",
    "environment.json",
    "TASK_SKILL.md",
    "sessions",
}


def export_native_evidence(output):
    """Export bounded exploratory changes, never Codex credentials or formal facts."""
    data = manifest()
    marker = CONTROL / "native-workspace.json"
    if not marker.exists() and not marker.is_symlink():
        raise ValueError("Native workspace has not been prepared")
    saved = trusted_json(marker)
    root = checked(CODEX, "workspace/candidate", exists=True)
    if any(
        saved["identity"].get(key) != data[key]
        for key in ("task_id", "run_id", "attempt_id")
    ) or saved["directories"] != native_directory_identity(root, data):
        raise ValueError("Native export identity changed")
    workspace = checked(CODEX, "workspace", exists=True)
    original = {
        "candidate/checkout/" + name: entry
        for name, entry in saved["source_files"].items()
    }
    entries, content, observed, total = {}, {}, set(), 0
    for parent, directories, files in os.walk(workspace, followlinks=False):
        directories[:] = [name for name in directories if name not in NATIVE_EXCLUDED]
        for name in [*directories, *files]:
            if name in NATIVE_EXCLUDED:
                continue
            path = Path(parent) / name
            relative = path.relative_to(workspace).as_posix()
            observed.add(relative)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                entry = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size > MAX_EXPORT:
                    raise ValueError("Native evidence file exceeds its bound")
                with os.fdopen(
                    os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb"
                ) as stream:
                    current = os.fstat(stream.fileno())
                    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                        raise ValueError("Native evidence file changed type")
                    payload = stream.read(MAX_EXPORT + 1)
                entry = {
                    "type": "file",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "executable": bool(info.st_mode & 0o111),
                }
                if entry != original.get(relative):
                    total += len(payload)
                    if total > MAX_EXPORT - 2 * 1024**2:
                        raise ValueError("Native evidence export exceeds its limit")
                    content[relative] = payload
            else:
                raise ValueError("Native evidence forbids hardlinks and special files")
            if entry != original.get(relative):
                entries[relative] = {
                    **entry,
                    "change": "modified" if relative in original else "added",
                }
    for name in sorted(set(original) - observed):
        if not any(part in NATIVE_EXCLUDED for part in Path(name).parts):
            entries[name] = {"change": "deleted", "original": original[name]}
    metadata = {
        "schema": "local-ci-native-evidence/v1",
        "exploratory_only": True,
        "identity": saved["identity"],
        "changes": entries,
        "excluded_names": sorted(NATIVE_EXCLUDED),
    }
    encoded = json.dumps(metadata, sort_keys=True).encode()
    if len(encoded) > 2 * 1024**2:
        raise ValueError("Native evidence metadata exceeds its limit")
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for name, payload in [
            ("native-manifest.json", encoded),
            *[("files/" + name, value) for name, value in sorted(content.items())],
        ]:
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(payload), 0o600
            archive.addfile(member, io.BytesIO(payload))


def deploy_session(payload):
    data = manifest()
    files = payload["files"]
    if set(files) - {"config.toml", "auth.json", "TASK_SKILL.md"}:
        raise ValueError("Only trusted Codex session files may be deployed")
    for name, text in files.items():
        if not isinstance(text, str) or len(text.encode()) > 4 * 1024**2:
            raise ValueError("Invalid session file")
        target = CODEX / ("workspace" if name == "TASK_SKILL.md" else "home") / name
        write(
            target,
            text.encode(),
            uid=data["uids"]["codex"],
            gid=data["gids"]["codex"],
            mode=0o600 if name != "TASK_SKILL.md" else 0o400,
        )
    environment = payload["environment"]
    if not isinstance(environment, dict) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or not isinstance(v, str)
        or "\x00" in v
        for k, v in environment.items()
    ):
        raise ValueError("Invalid private session environment")
    write(
        CODEX / "environment.json",
        json.dumps(environment).encode(),
        uid=data["uids"]["codex"],
        gid=data["gids"]["codex"],
    )
    return {
        "home": "/codex/home",
        "workspace": "/codex/workspace",
        "python_bin": data.get("python_bin", "python3"),
        "mcp_script": "/opt/local-ci/control/scripts/local_ci/agent_ci/mcp_server.py",
    }


def purge_credentials():
    for path in (
        CODEX / "home/config.toml",
        CODEX / "home/auth.json",
        CODEX / "environment.json",
    ):
        if path.is_symlink():
            raise ValueError("Refusing symlinked credentials")
        path.unlink(missing_ok=True)
    return {"purged": True}


def runtime_info():
    result = {}
    environment = manifest().get("env", {})
    seed = environment.get("SEED_PYTHON")
    if not seed:
        activation = environment.get("PYTHON_VENV_ACTIVATE")
        seed = str(Path(activation).parent / "python") if activation else "python3"
    for variant in ("candidate", "base"):
        root = checked(TASK, variant + "/venv")
        marker = checked(root, ".local-ci-environment.json")
        available = (root / "bin/python").is_file()
        result[variant] = {
            "python_available": available,
            "runtime_origin": "variant" if available else "seed",
            "python_bin": str(root / "bin/python") if available else seed,
            "marker": json.loads(marker.read_text()) if marker.is_file() else None,
        }
    return result


def read_file(params):
    scope = params["scope"]
    if scope not in {"candidate", "base", "experiments", "artifacts"}:
        raise ValueError("Invalid readable task scope")
    path = checked(TASK / scope, params["path"], exists=True)
    if not path.is_file() or path.stat().st_size > 2 * 1024**2:
        raise ValueError("Readable object must be a bounded regular file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        return {"base64": base64.b64encode(stream.read()).decode()}


def prepare_image():
    recipe = json.loads(Path("/opt/local-ci/image-recipe.json").read_text())
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        **recipe["env"],
    }
    llvm = recipe["llvm"]
    if llvm["mode"] == "source":
        source = "/opt/local-ci/runtime/deps/llvm-source/llvm"
        build = "/opt/local-ci/runtime/deps/llvm-build"
        arguments = llvm.get(
            "cmake_args",
            [
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLVM_ENABLE_PROJECTS=mlir;clang;lld",
                "-DLLVM_TARGETS_TO_BUILD=host;NVPTX;AMDGPU",
            ],
        )
        subprocess.run(
            [
                "cmake",
                "-S",
                source,
                "-B",
                build,
                *arguments,
                "-DCMAKE_INSTALL_PREFIX=" + environment["LLVM_BUILD_DIR"],
            ],
            check=True,
            env=environment,
        )
        subprocess.run(
            [
                "cmake",
                "--build",
                build,
                "--target",
                "install",
                "--parallel",
                str(recipe.get("build_jobs", 8)),
            ],
            check=True,
            env=environment,
        )
        shutil.rmtree(build)
        shutil.rmtree("/opt/local-ci/runtime/deps/llvm-source")
    for command in recipe.get("prepare_commands", []):
        subprocess.run(
            command, check=True, env=environment, cwd="/opt/local-ci/runtime"
        )
    # Docker COPY preserves host umask; every non-root role must be able to
    # read trusted image dependencies, without gaining write permission.
    for root in (Path("/opt/local-ci/runtime"), Path("/opt/local-ci/control")):
        for path in [root, *walk(root)]:
            mode = path.stat().st_mode
            own(path, 0, 0, 0o755 if path.is_dir() or mode & 0o111 else 0o644)
    return {"prepared": True}


def task_usage():
    return {
        "bytes": sum(
            path.stat().st_size
            for root in (TASK, CODEX)
            for path in walk(root)
            if path.is_file()
        )
    }


def main():
    operation = sys.argv[1]
    if operation == "launch-codex":
        environment = json.loads((CODEX / "environment.json").read_text())
        executable = environment.pop("LOCAL_CI_CODEX_BIN")
        if not Path(executable).is_absolute():
            raise ValueError("Codex executable must be an absolute trusted image path")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0):
            raise OSError("Could not set no_new_privs")
        os.execvpe(executable, [executable, *sys.argv[2:]], environment)
    if os.geteuid() != 0:
        raise ValueError("Management helper requires container namespace UID 0")
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    if operation == "prepare-image":
        result = prepare_image()
    elif operation in {"init", "deploy-session"}:
        payload = json.load(sys.stdin)
        result = init_task(payload) if operation == "init" else deploy_session(payload)
    elif operation == "import-checkout":
        result = import_checkout(params, sys.stdin.buffer)
    elif operation == "prepare-execution":
        result = prepare_execution(params)
    elif operation == "write-execution-file":
        result = write_execution_file(params, sys.stdin.buffer.read(2 * 1024**2 + 1))
    elif operation in {"export-execution", "export-evidence"}:
        root = TASK / "artifacts"
        if operation == "export-execution":
            root = checked(
                root, safe_name(params["execution_id"], r"[a-f0-9]{32}"), exists=True
            )
        export_archive(
            root, sys.stdout.buffer, exclude=("execution.log", "executor-record.json")
        )
        return 0
    elif operation == "read-file":
        result = read_file(params)
    elif operation == "runtime-info":
        result = runtime_info()
    elif operation == "verify-checkout":
        result = verify_checkout(params)
    elif operation == "authorize-diagnostics":
        result = authorize_diagnostics()
    elif operation == "create-experiment":
        result = create_experiment(params)
    elif operation == "prepare-native-workspace":
        result = prepare_native_workspace(params)
    elif operation == "export-native-evidence":
        export_native_evidence(sys.stdout.buffer)
        return 0
    elif operation == "purge-credentials":
        result = purge_credentials()
    elif operation == "write-baseline":
        path = checked(
            TASK, ".trusted/baselines/" + safe_name(params["tool_id"]) + ".json"
        )
        path.parent.mkdir(exist_ok=True)
        own(path.parent, 0, 0, 0o755)
        payload = sys.stdin.buffer.read(2 * 1024**2 + 1)
        if len(payload) > 2 * 1024**2 or not isinstance(json.loads(payload), dict):
            raise ValueError("Invalid bounded baseline document")
        write(path, payload, mode=0o444)
        result = {"path": str(path)}
    elif operation == "task-usage":
        result = task_usage()
    else:
        raise ValueError("Unsupported fixed management operation")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Trusted container management operation failed", file=sys.stderr)
        raise SystemExit(1)
