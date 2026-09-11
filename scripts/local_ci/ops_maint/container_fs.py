#!/usr/bin/env python3
"""Fixed management operations inside one rootless PR container.

Never accepts host paths, arbitrary UIDs or arbitrary management commands.
Session credentials arrive on stdin and remain outside exported artifacts.
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
SESSION = TASK / "session"
CONTROL = TASK / ".control"
MAX_ARCHIVE = 4 * 1024**3
MAX_EXPORT = 512 * 1024**2
ROLES = ("task",)


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
        if manifest() != payload:
            raise ValueError("Task identity cannot change within a run")
        return {"initialized": True}
    uids, gids = payload["uids"], payload["gids"]
    if (
        set(uids) != {"task"}
        or set(gids) != {"task"}
        or any(type(v) is not int or v <= 0 for v in [*uids.values(), *gids.values()])
    ):
        raise ValueError("One non-root task UID and GID are required")
    TASK.mkdir(parents=True, exist_ok=True)
    CONTROL.mkdir(exist_ok=True)
    own(CONTROL, 0, 0, 0o700)
    for name in (
        "candidate",
        "base",
        "artifacts",
        "experiments",
        "session",
        "session/home",
        ".trusted",
    ):
        root = TASK / name
        root.mkdir(parents=True, exist_ok=True)
        if name == "artifacts":
            own(root, 0, 0, 0o777)
        else:
            own(
                root,
                uids["task"],
                gids["task"],
                0o755 if name != "session/home" else 0o700,
            )
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
    set_tree_identity(destination, data["uids"]["task"], data["gids"]["task"])
    backend = data.get("env", {}).get("BACKEND_PATH")
    if data.get("backend_enabled") and backend:
        target = checked(TASK, variant + "/backend")
        shutil.copytree(backend, target, symlinks=True)
        set_tree_identity(target, data["uids"]["task"], data["gids"]["task"])
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
    changed = {}
    for path, entry in saved["files"].items():
        try:
            current = source_entry(root, path)
        except (OSError, ValueError):
            current = {"type": "missing_or_unreadable"}
        if current != entry:
            changed[path] = {"before": entry, "after": current}
    verified = saved["sha"] == params["expected_sha"] and not changed
    patch_digest = (
        hashlib.sha256(
            json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if changed
        else None
    )
    return {
        "verified": bool(verified),
        "sha": saved["sha"],
        "dirty": bool(changed),
        "clean": bool(verified),
        "head": saved["sha"],
        "changed_files": sorted(changed),
        "patch_digest": patch_digest,
    }


def prepare_execution(params):
    data = manifest()
    ident = safe_name(params["execution_id"], r"[a-f0-9]{32}")
    variant = params["variant"]
    if variant not in {"candidate", "base"}:
        raise ValueError("Invalid source variant")
    root = checked(TASK, "artifacts/" + ident)
    scripts = checked(TASK, ".trusted/scripts/" + ident)
    base = checked(TASK, variant)
    for path in (
        root,
        scripts,
        base,
        *[base / n for n in ("home", "tmp", "cache", "state")],
    ):
        path.mkdir(parents=True, exist_ok=True)
        if path == root:
            own(path, 0, 0, 0o777)
        else:
            own(path, data["uids"]["task"], data["gids"]["task"], 0o755)
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
    write(root / name, content, gid=manifest()["gids"]["task"], mode=0o440)
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


def authorize_diagnostics():
    return {"authorized": True}


def create_experiment(params):
    data = manifest()
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
    set_tree_identity(root, data["uids"]["task"], data["gids"]["task"])
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
    # Debian dist-packages. The immutable seed is used only for environment preparation.
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


def prepare_workspace(params):
    """Prepare one data version once; reject partial or mismatched environments."""
    data = manifest()
    variant = params.get("variant", "candidate")
    if variant not in {"candidate", "base"}:
        raise ValueError("Unknown task data version")
    root = checked(TASK, variant, exists=True)
    fingerprint = params["environment_fingerprint"]
    venv = root / "venv"
    marker = venv / ".local-ci-environment.json"
    expected = {"environment_fingerprint": fingerprint}
    reused = venv.exists()
    if reused:
        if (
            not (venv / "bin/python").is_file()
            or not marker.is_file()
            or marker.is_symlink()
            or json.loads(marker.read_text()) != expected
        ):
            raise ValueError("Partial or different task venv requires a new run")
    else:
        seed_venv(root, data.get("env", {}))
        write(marker, json.dumps(expected, sort_keys=True).encode(), mode=0o644)
        set_tree_identity(venv, data["uids"]["task"], data["gids"]["task"])
    for name in ("home", "tmp", "cache", "state"):
        (root / name).mkdir(exist_ok=True)
        own(root / name, data["uids"]["task"], data["gids"]["task"], 0o755)
    return {
        **native_layout(root),
        "reused": reused,
        "environment_fingerprint": fingerprint,
    }


def prepare_native_workspace(params):
    return {
        **prepare_workspace({**params, "variant": "candidate"}),
        "source_sha": params["expected_sha"],
    }


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
    "AI_CI_PROGRAM.md",
    "sessions",
}


def export_native_evidence(output):
    """Record current source changes for attribution to the modified version."""
    saved = trusted_json(CONTROL / "candidate-source-manifest.json")
    root = checked(TASK, "candidate/checkout", exists=True)
    changes, content = {}, {}
    for name, original in saved["files"].items():
        try:
            current = source_entry(root, name)
        except FileNotFoundError:
            changes[name] = {"change": "deleted", "original": original}
            continue
        if current != original:
            changes[name] = {"change": "modified", **current}
            if current["type"] == "file":
                content[name] = (root / name).read_bytes()
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [n for n in directories if n not in NATIVE_EXCLUDED]
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative not in saved["files"] and not path.is_symlink():
                changes[relative] = {"change": "added", **source_entry(root, relative)}
                content[relative] = path.read_bytes()
    payload = json.dumps(
        {
            "schema": "local-ci-source-changes/v1",
            "source_sha": saved["sha"],
            "changes": changes,
        },
        sort_keys=True,
    ).encode()
    if sum(map(len, content.values())) + len(payload) > MAX_EXPORT:
        raise ValueError("Source evidence export exceeds its limit")
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for name, data in [
            ("native-manifest.json", payload),
            *[("files/" + k, v) for k, v in content.items()],
        ]:
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o600
            archive.addfile(member, io.BytesIO(data))


def deploy_session(payload):
    data = manifest()
    files = payload["files"]
    if set(files) - {"config.toml", "auth.json", "AI_CI_PROGRAM.md"}:
        raise ValueError("Only trusted Codex session files may be deployed")
    for name, text in files.items():
        if not isinstance(text, str) or len(text.encode()) > 4 * 1024**2:
            raise ValueError("Invalid session file")
        target = (
            TASK / "candidate/checkout"
            if name == "AI_CI_PROGRAM.md"
            else SESSION / "home"
        ) / name
        write(
            target,
            text.encode(),
            uid=data["uids"]["task"],
            gid=data["gids"]["task"],
            mode=0o600 if name != "AI_CI_PROGRAM.md" else 0o400,
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
        SESSION / "environment.json",
        json.dumps(environment).encode(),
        uid=data["uids"]["task"],
        gid=data["gids"]["task"],
    )
    return {
        "home": "/task/session/home",
        "workspace": "/task/candidate/checkout",
        "python_bin": data.get("python_bin", "python3"),
        "mcp_script": "/opt/local-ci/control/scripts/local_ci/agent_ci/mcp_server.py",
    }


def purge_credentials():
    for path in (
        SESSION / "home/config.toml",
        SESSION / "home/auth.json",
        SESSION / "environment.json",
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


def task_usage():
    return {
        "bytes": sum(
            path.stat().st_size
            for root in (TASK,)
            for path in walk(root)
            if path.is_file()
        )
    }


def main():
    operation = sys.argv[1]
    if operation == "launch-codex":
        environment = json.loads((SESSION / "environment.json").read_text())
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
    if operation in {"init", "deploy-session"}:
        payload = json.load(sys.stdin)
        result = init_task(payload) if operation == "init" else deploy_session(payload)
    elif operation == "import-checkout":
        result = import_checkout(params, sys.stdin.buffer)
    elif operation == "prepare-execution":
        result = prepare_execution(params)
    elif operation == "write-execution-file":
        result = write_execution_file(params, sys.stdin.buffer.read(2 * 1024**2 + 1))
    elif operation == "make-artifacts-readable":
        root = checked(
            TASK / "artifacts",
            safe_name(params["execution_id"], r"[a-f0-9]{32}"),
            exists=True,
        )
        for path in walk(root, reject_links=True):
            path.chmod(0o755 if path.is_dir() else 0o644)
        root.chmod(0o777)
        result = {"exported": True}
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
    elif operation == "prepare-workspace":
        result = prepare_workspace(params)
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
    elif operation == "clean-work":
        preserved = set() if params.get("scratch_only") else {"artifacts", "rpc"}
        for path in TASK.iterdir():
            if path.name not in preserved:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        result = {"cleaned": True}
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
