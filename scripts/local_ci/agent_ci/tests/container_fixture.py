"""Rootless task-volume boundary fixture; real POSIX files, UIDs and processes.

No daemon, mount, namespace or compiler is started. Container paths are mapped
to per-attempt temporary directories before the fake docker drops its UID.
The expensive seed package copy is replaced by a real empty, independent venv.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path, PurePosixPath

from test_agent_ci import FakeManager
from agent_ci.executor import STOP_PROGRAM
from agent_ci.protocol import ContractError


def git(cwd, *arguments):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(cwd), '-c', 'core.fsmonitor=false',
        '-c', 'core.hooksPath=/dev/null', *arguments], cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()


FAKE_DOCKER = r'''#!/usr/bin/python3
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
assert args.pop(0)=='--host'
endpoint=args.pop(0)
assert endpoint.startswith('unix://')
assert args.pop(0)=='exec'
user=None
while args and args[0].startswith('-'):
    key=args.pop(0)
    if key=='--user': user=args.pop(0)
    elif key in ('-i','--interactive'): pass
    else: raise RuntimeError('Unsupported fixture Docker option: '+key)
container=args.pop(0)
with open(__file__+'.layout') as stream: layout=json.load(stream)[container]
def mapped(value):
    for source,target in (('/run/local-ci-rpc',layout['rpc_host_dir']),('/codex',layout['codex_root']),('/task',layout['task_root'])):
        value=value.replace(source,target)
    return value
args=[mapped(value) for value in args]
env={}
if args[:2]==['env','-i']:
    args=args[2:]
    while args and '=' in args[0] and not args[0].startswith('-'):
        k,v=args.pop(0).split('=',1);env[k]=v
else:
    env={'PATH':os.defpath,'LANG':'C.UTF-8'}
with open(__file__+'.calls','a') as log:
    log.write(json.dumps({'container':container,'endpoint':endpoint,'user':user,'env':env,'args':args})+'\n')
uid,gid=map(int,user.split(':'))
assert uid==0 or uid in layout['uids'].values()
if os.geteuid()!=0: raise RuntimeError('Fixture needs root only to simulate private non-host UIDs')
os.setgroups([]);os.setgid(gid);os.setuid(uid);os.umask(0o027)
prefix=[];nested=args
if len(args)>5 and args[1:4]==['-I','-S','-c'] and 'PR_SET_NO_NEW_PRIVS' in args[4]:
    prefix=args[:5];nested=args[5:]
if len(nested)>3 and nested[1]=='-c' and 'marker=target/' in nested[2]:
    assert '--system-site-packages' not in nested[2] and '--copies' in nested[2]
    seed=r"""import json,pathlib,subprocess,sys,sysconfig
p=pathlib.Path(sys.argv[1])
if not (p/'bin/python').exists(): subprocess.run([sys.executable,'-m','venv','--without-pip','--copies',str(p)],check=True)
(p/'.local-ci-environment.json').write_text(json.dumps({'fingerprint':sys.argv[2]}))
if sys.argv[3]=='metadata':
    site=pathlib.Path(subprocess.check_output([str(p/'bin/python'),'-I','-c','import sysconfig;print(sysconfig.get_path("purelib"))']).decode().strip())
    for name in ('build','setuptools','wheel','pybind11'):
        info=site/(name+'-0.0.dist-info');info.mkdir(exist_ok=True)
        (info/'METADATA').write_text('Metadata-Version: 2.1\nName: '+name+'\nVersion: 0.0\n')
"""
    args=prefix+[nested[0],'-c',seed,*nested[3:], 'metadata' if layout.get('seed_metadata') else 'empty']
os.execvpe(args[0],args,env)
'''


class VolumeManager(FakeManager):
    def __init__(self, root: Path, backend=False, *, docker_bin=None, seed_metadata=False):
        super().__init__(Path(root), backend=backend)
        self.docker_bin = Path(docker_bin or self.root / 'fake-docker')
        if not self.docker_bin.exists():
            self.docker_bin.write_text(FAKE_DOCKER)
            self.docker_bin.chmod(0o755)
        self.seed_metadata = seed_metadata
        self.volume_root = self.root / 'volumes'
        self.volume_root.mkdir(exist_ok=True)
        self.root.chmod(0o755)
        self.volume_root.chmod(0o711)
        self.imports = []

    def volume(self, handle):
        return self.volume_root / handle['attempt_id'] / 'task'

    def codex_volume(self, handle):
        return self.volume_root / handle['attempt_id'] / 'codex'

    @staticmethod
    def identities():
        occupied = set()
        for file in Path('/proc').glob('[0-9]*/status'):
            try:
                for line in file.read_text().splitlines():
                    if line.startswith('Uid:'):
                        occupied.update(map(int, line.split()[1:]))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        while True:
            first = 100_000_000 + int(uuid.uuid4().hex[:8], 16) % 1_000_000_000
            values = list(range(first, first + 4))
            if occupied.intersection(values):
                continue
            try:
                for value in values:
                    try:
                        pwd.getpwuid(value)
                    except KeyError:
                        continue
                    raise ValueError('host UID')
            except ValueError:
                continue
            return dict(zip(('candidate', 'base', 'diagnostic', 'codex'), values))

    @staticmethod
    def owned(path, uid, gid, mode=0o750):
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        path.chmod(mode)

    def acquire(self, *args):
        handle = super().acquire(*args)
        uids = self.identities()
        gids = {role: uids['candidate'] for role in uids}
        gids['codex'] = uids['codex']
        handle.update(uids=uids, gids=gids, execution_uid=uids['candidate'], execution_gid=gids['candidate'],
                      execution_user=f"{uids['candidate']}:{gids['candidate']}",
                      env={'SEED_PYTHON': sys.executable}, volumes={'task': 'task-' + handle['attempt_id'],
                                                                 'codex': 'codex-' + handle['attempt_id']})
        self.owned(self.volume(handle).parent, 0, 0, 0o711)
        for root in (self.volume(handle), self.codex_volume(handle)):
            self.owned(root, 0, 0, 0o711)
        for variant in ('candidate', 'base'):
            self.owned(self.volume(handle) / variant, uids[variant], gids[variant], 0o2750)
            for name in ('home', 'tmp', 'state', 'cache'):
                self.owned(self.volume(handle) / variant / name, uids[variant], gids[variant], 0o2750)
        for name in ('artifacts', 'diagnostics', 'experiments', '.trusted', '.trusted/scripts', '.trusted/baselines'):
            self.owned(self.volume(handle) / name, 0, 0, 0o711)
        self.owned(self.codex_volume(handle) / 'home', uids['codex'], gids['codex'], 0o700)
        self.owned(self.codex_volume(handle) / 'workspace', 0, gids['codex'], 0o750)
        return handle

    def acquire_task(self, task, run_id, *, rpc_directory):
        handle = super().acquire_task(task, run_id, rpc_directory=rpc_directory)
        self.publish_layout(handle)
        return handle

    def publish_layout(self, handle):
        path = Path(str(self.docker_bin) + '.layout')
        values = json.loads(path.read_text()) if path.exists() else {}
        values[handle['container_id']] = {'task_root': str(self.volume(handle)), 'codex_root': str(self.codex_volume(handle)),
            'rpc_host_dir': handle.get('rpc_host_dir', str(self.root / 'rpc')), 'uids': handle['uids'],
            'seed_metadata': self.seed_metadata}
        path.write_text(json.dumps(values))
        path.chmod(0o600)

    def translate(self, handle, value):
        return value.replace('/run/local-ci-rpc', handle.get('rpc_host_dir', str(self.root / 'rpc'))) \
                    .replace('/codex', str(self.codex_volume(handle))).replace('/task', str(self.volume(handle)))

    @staticmethod
    def assign_tree(root, uid, gid):
        for directory, _, names in os.walk(root, followlinks=False):
            path = Path(directory)
            os.chown(path, uid, gid)
            path.chmod(0o2750)
            for name in names:
                child = path / name
                info = child.lstat()
                if stat.S_ISREG(info.st_mode):
                    os.chown(child, uid, gid)
                    child.chmod(0o750 if info.st_mode & 0o111 else 0o640)
                elif not stat.S_ISLNK(info.st_mode):
                    raise ContractError('Non-regular fixture input')

    def import_checkout(self, handle, variant, archive, digest):
        if variant not in ('candidate', 'base') or hashlib.sha256(Path(archive).read_bytes()).hexdigest() != digest:
            raise ContractError('Invalid checkout import')
        target = self.volume(handle) / variant / 'checkout'
        target.mkdir()
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts or member.islnk():
                    raise ContractError('Invalid checkout archive path')
                path = target / member.name
                if not path.parent.resolve().is_relative_to(target.resolve()) and path != target:
                    raise ContractError('Checkout archive escaped through symlink')
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as source, path.open('wb') as destination:
                        shutil.copyfileobj(source, destination)
                    path.chmod(member.mode & 0o777)
                elif member.issym():
                    path.symlink_to(member.linkname)
                else:
                    raise ContractError('Unsupported checkout member')
        self.assign_tree(target, handle['uids'][variant], handle['gids'][variant])
        self.imports.append((handle['attempt_id'], variant, digest))

    def prepare_execution(self, handle, execution_id, variant, *, diagnostic=False):
        role = 'diagnostic' if diagnostic else variant
        self.owned(self.volume(handle) / 'artifacts' / execution_id, handle['uids'][role], handle['gids'][role])
        if diagnostic:
            root = self.volume(handle) / 'diagnostics' / execution_id
            for name in ('', 'home', 'tmp', 'cache'):
                self.owned(root / name, handle['uids'][role], handle['gids'][role], 0o700)

    def write_execution_file(self, handle, execution_id, name, content):
        if Path(name).name != name:
            raise ContractError('Invalid script name')
        root = self.volume(handle) / '.trusted/scripts' / execution_id
        self.owned(root, 0, 0, 0o711)
        path = root / name
        path.write_text(self.translate(handle, content))
        path.chmod(0o444)

    def runtime_info(self, handle):
        result = {}
        for variant in ('candidate', 'base'):
            available = (self.volume(handle) / variant / 'venv/bin/python').is_file()
            result[variant] = {'python_available': available, 'runtime_origin': 'variant' if available else 'seed',
                               'python_bin': '/task/' + variant + '/venv/bin/python' if available else sys.executable}
        return result

    def authorize_diagnostics(self, handle):
        # Fixed formal roots only; never follow links to credentials or /proc.
        for variant in ('candidate', 'base'):
            self.assign_tree(self.volume(handle) / variant, handle['uids'][variant], handle['gids'][variant])

    def verify_checkout(self, handle, variant, expected_sha):
        root = self.volume(handle) / variant / 'checkout'
        actual = git(root, 'rev-parse', 'HEAD')
        dirty = bool(git(root, 'status', '--porcelain', '--untracked-files=no'))
        return {'verified': actual == expected_sha, 'sha': actual, 'dirty': dirty}

    def export_execution(self, handle, execution_id, destination):
        source = self.volume(handle) / 'artifacts' / execution_id
        for path in source.rglob('*'):
            relative = path.relative_to(source)
            if relative.as_posix() in {'execution.log', 'executor-record.json'}:
                continue
            info = path.lstat()
            if path.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ContractError('Unsafe execution export')
            target = Path(destination) / relative
            if path.is_dir():
                target.mkdir(exist_ok=True)
            else:
                if info.st_nlink != 1:
                    raise ContractError('Hardlinked execution export')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

    def write_baseline(self, handle, tool_id, value):
        path = self.volume(handle) / '.trusted/baselines' / (tool_id + '.json')
        path.write_text(json.dumps(value))
        path.chmod(0o444)

    def create_experiment(self, handle, experiment_id, variant):
        root = self.volume(handle) / 'experiments' / experiment_id
        markers = self.volume(handle) / '.trusted/experiments'
        markers.mkdir(exist_ok=True)
        marker = markers / (experiment_id + '.json')
        if root.exists():
            if root.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != {'variant': variant}:
                raise ContractError('Experiment belongs to another variant')
            return
        root.mkdir()
        shutil.copytree(self.volume(handle) / variant / 'checkout', root / 'checkout', symlinks=True)
        original = self.volume(handle) / variant / 'venv'
        if original.is_dir():
            shutil.copytree(original, root / 'venv', symlinks=True)
        else:
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--copies', str(root / 'venv')], check=True)
        marker.write_text(json.dumps({'variant': variant}))
        self.assign_tree(root, handle['uids']['diagnostic'], handle['gids']['diagnostic'])

    def deploy_session(self, handle, files, environment):
        root = self.codex_volume(handle)
        for name, data in files.items():
            if name not in {'config.toml', 'auth.json', 'TASK_SKILL.md'}:
                raise ContractError('Unknown private session file')
            target = root / ('workspace' if name == 'TASK_SKILL.md' else 'home') / name
            target.write_text(self.translate(handle, data))
            if name == 'TASK_SKILL.md':
                target.chmod(0o444)
            else:
                os.chown(target, handle['uids']['codex'], handle['gids']['codex'])
                target.chmod(0o600)
        if environment:
            target = root / 'environment.json'
            target.write_text(json.dumps({key: self.translate(handle, value) for key, value in environment.items()}))
            os.chown(target, handle['uids']['codex'], handle['gids']['codex'])
            target.chmod(0o600)

    def purge_credentials(self, handle):
        super().purge_credentials(handle)
        for relative in ('environment.json', 'home/auth.json'):
            (self.codex_volume(handle) / relative).unlink(missing_ok=True)

    def stop_task(self, handle):
        if self.validation_failure:
            raise RuntimeError(self.validation_failure)
        for uid in handle['uids'].values():
            result = subprocess.run([sys.executable, '-I', '-S', '-c', STOP_PROGRAM, str(uid)], capture_output=True)
            if result.returncode or not json.loads(result.stdout).get('verified'):
                raise ContractError('Fixture task process cleanup failed')
        return {'verified': True}

    def destroy_task(self, handle, *, keep_data=True):
        result = super().destroy_task(handle, keep_data=keep_data)
        if not keep_data:
            root = self.volume(handle).parent
            if root.exists():
                if root.parent != self.volume_root or root.is_symlink():
                    raise ContractError('Invalid fixture volume removal')
                shutil.rmtree(root)
        return result

    def task_usage(self, handle):
        root = self.volume(handle).parent
        return sum(path.lstat().st_size for path in root.rglob('*') if path.is_file() and not path.is_symlink())
