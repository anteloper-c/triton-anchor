"""Linux integration boundaries: real shell/Python processes, no Docker daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.executor import DockerExecutor, VENV_PROGRAM
from agent_ci.protocol import ContractError


FAKE_DOCKER = r'''#!/usr/bin/python3
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
assert args.pop(0)=='exec'
assert args.pop(0)=='--user'
user=args.pop(0)
args.pop(0)
assert args[:2]==['env','-i']; args=args[2:]
env={}
while args and '=' in args[0]:
    k,v=args.pop(0).split('=',1);env[k]=v
with open(__file__+'.calls','a') as log: log.write(json.dumps({'user':user,'env':env,'args':args})+'\n')
if os.geteuid()==0:
    uid,gid=map(int,user.split(':'));os.setgroups([]);os.setgid(gid);os.setuid(uid)
if len(args)>3 and args[1]=='-c' and 'marker=target/' in args[2]:
    assert '--system-site-packages' not in args[2] and '--copies' in args[2]
    # The expensive seeded package copy is the fake boundary. venv creation is real.
    args=[args[0],'-c',"import json,pathlib,subprocess,sys; p=pathlib.Path(sys.argv[1]); subprocess.run([sys.executable,'-m','venv','--without-pip','--copies',str(p)],check=True) if not (p/'bin/python').exists() else None; (p/'.local-ci-environment.json').write_text(json.dumps({'fingerprint':sys.argv[2]}))",*args[3:]]
os.execvpe(args[0],args,env)
'''

FAKE_TOOL = r'''#!/usr/bin/python3
import json,os,pathlib,sys
tool=sys.argv[1];art=pathlib.Path(os.environ['LOCAL_CI_ARTIFACT_DIR'])
if os.environ.get('FAKE_RESULT')!='missing':
    (art/'result.json').write_text(json.dumps({'tool_id':tool,'status':'pass','tested_sha':os.environ['LOCAL_CI_TESTED_SHA'],'environment_fingerprint':os.environ['LOCAL_CI_ENVIRONMENT_FINGERPRINT']}))
if tool in ('compile_time','pass_profile','ir_serialization'):
    (art/'candidate.json').write_text(json.dumps({'metadata':{'commit_sha':os.environ['LOCAL_CI_TESTED_SHA'],'environment_fingerprint':os.environ['LOCAL_CI_ENVIRONMENT_FINGERPRINT']},'rows':[{'value':1}]}))
print(json.dumps({'baseline':os.environ.get('BASELINE_JSON'),'max_jobs':os.environ['MAX_JOBS'],'uid':os.geteuid()}))
sys.exit(int(os.environ.get('FAKE_EXIT','0')))
'''


def git(cwd, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Boundary Test', '-c', 'user.email=boundary@example.invalid',
                                    '-c', 'safe.directory=' + str(cwd), *args], cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()


class LocalRelay:
    def __init__(self, source):
        self.source = source

    def checkout(self, sha, target):
        if not target.exists():
            git(self.source, 'clone', '--quiet', '--no-hardlinks', str(self.source), str(target))
            git(target, 'checkout', '--quiet', '--detach', sha)
        elif git(target, 'rev-parse', 'HEAD') != sha:
            raise ContractError('frozen checkout differs')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process/ownership integration')
class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='local-ci-executor-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-q')
        (self.source / 'README.md').write_text('base\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'base')
        base = git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'README.md').write_text('candidate\n')
        git(self.source, 'commit', '-qam', 'candidate')
        candidate = git(self.source, 'rev-parse', 'HEAD')
        self.fake = self.root / 'fake-docker'
        self.fake.write_text(FAKE_DOCKER)
        self.fake.chmod(0o755)
        self.control = self.root / 'control/scripts/local_ci/tools'
        self.control.mkdir(parents=True)
        (self.control / 'fake.py').write_text(FAKE_TOOL)
        (self.control / 'run_tool.sh').write_text('#!/bin/bash\nexec /usr/bin/python3 "$(dirname "$0")/fake.py" "$1"\n')
        uid = 65534 if os.geteuid() == 0 else os.geteuid()
        gid = 65534 if os.geteuid() == 0 else os.getegid()
        self.task = {'task_id': 'a' * 64, 'base_sha': base, 'tested_sha': candidate,
                     'llvm_hash': 'b' * 40, 'full': False, 'worker_revision_sha': 'c' * 40}
        self.generation = {'workspace_host': str(self.root / 'workspace'), 'workspace_container': str(self.root / 'workspace'),
                           'container': 'fake-persistent', 'profile': 'triton30', 'backend_enabled': False,
                           'execution_user': f'{uid}:{gid}', 'execution_uid': uid, 'execution_gid': gid,
                           'environment_fingerprint': 'environment-v1',
                           'env': {'SEED_PYTHON': sys.executable, 'PYTHON_VENV_ACTIVATE': '/profile/seed/bin/activate'}}
        self.config = {'simulation': True, 'docker_bin': str(self.fake), 'container_control_root': str(self.root / 'control')}
        self.executor = DockerExecutor(self.config, self.root / 'state', self.generation, self.task, LocalRelay(self.source))

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, tool='environment', variant='candidate', parameters=None, custom=None, cancelled=None):
        return self.executor.run(tool, uuid.uuid4().hex, variant, parameters or {}, cancelled or threading.Event(), custom)

    def calls(self):
        return [json.loads(line) for line in Path(str(self.fake) + '.calls').read_text().splitlines()]

    def test_real_exit_and_clean_environment(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'must-not-enter', 'CONTROL_TOKEN': 'private'}):
            record = self.run_tool(parameters={'max_jobs': 3})
        self.assertEqual('pass', record['status'], record)
        self.assertEqual(self.task['worker_revision_sha'], record['worker_revision_sha'])
        calls = self.calls()
        for call in calls:
            self.assertNotIn('OPENAI_API_KEY', call['env'])
            self.assertNotIn('CONTROL_TOKEN', call['env'])
        call = next(call for call in calls if call['env'].get('ANCHOR_DIR'))
        self.assertEqual('3', call['env']['MAX_JOBS'])
        self.assertEqual('3', call['env']['CMAKE_BUILD_PARALLEL_LEVEL'])
        self.assertEqual('-j3', call['env']['NINJAFLAGS'])
        self.generation['env']['FAKE_EXIT'] = '7'
        failed = self.run_tool()
        self.assertEqual(('fail', 7), (failed['status'], failed['exit_code']))

    def test_missing_builtin_receipt_is_infrastructure_failure(self):
        self.generation['env']['FAKE_RESULT'] = 'missing'
        record = self.run_tool()
        self.assertEqual(('infra_error', 'tool_result_missing'), (record['status'], record['reason']))

    def test_custom_python_isolated_and_source_only(self):
        (self.source / 'json.py').write_text('raise RuntimeError("cwd import leak")')
        custom = {'name': 'check.py', 'language': 'python', 'source_only': True,
                  'content': 'import json,sys; print(json.dumps({"isolated":sys.flags.isolated,"no_site":sys.flags.no_site}))'}
        record = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', record['status'], record)
        self.assertTrue(record['source_only'])
        self.assertIn('-I', record['command'])
        self.assertIn('-S', record['command'])
        self.assertIn('"no_site": 1', (Path(record['artifact_dir']) / 'execution.log').read_text())
        custom.update(source_only=False)
        installed = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', installed['status'], installed)
        self.assertNotIn('-S', installed['command'])

    def test_variant_venvs_do_not_share_installed_modules(self):
        base = self.run_tool(variant='base')
        candidate = self.run_tool()
        self.assertEqual(('pass', 'pass'), (base['status'], candidate['status']))
        root = self.executor.host_root
        base_python = root / 'base/venv/bin/python'
        candidate_python = root / 'candidate/venv/bin/python'
        site = Path(subprocess.check_output([str(base_python), '-I', '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))']).decode().strip())
        (site / 'only_base_package.py').write_text('VALUE = 42\n')
        self.assertEqual(0, subprocess.run([str(base_python), '-I', '-c', 'import only_base_package']).returncode)
        code = subprocess.run([str(candidate_python), '-I', '-c', 'import only_base_package'], capture_output=True).returncode
        self.assertNotEqual(0, code)
        self.assertNotIn('--system-site-packages', VENV_PROGRAM)
        self.assertIn('--reflink=auto', VENV_PROGRAM)

    def test_baseline_is_registered_and_sealed(self):
        self.assertEqual('base_check_not_executed', self.executor.get_baseline('compile_time')['reason'])
        base = self.run_tool('compile_time', 'base')
        self.assertEqual('pass', base['status'], base)
        self.assertEqual('available', self.executor.get_baseline('compile_time')['status'])
        candidate = self.run_tool('compile_time')
        self.assertEqual('pass', candidate['status'], candidate)
        call = next(call for call in reversed(self.calls()) if call['env'].get('BASELINE_JSON'))
        self.assertIn('/.trusted/baselines/', call['env']['BASELINE_JSON'])
        sealed = json.loads(Path(call['env']['BASELINE_JSON']).read_text())
        self.assertEqual(self.task['base_sha'], sealed['metadata']['commit_sha'])
        (Path(base['artifact_dir']) / 'candidate.json').write_text('{}')
        with self.assertRaisesRegex(ContractError, 'changed'):
            self.executor.set_baseline('compile_time', base['execution_id'])
        self.assertEqual('available', self.executor.get_baseline('compile_time')['status'])

    def test_cancel_kills_detached_child_inside_execution(self):
        cancelled = threading.Event()
        script = {'name': 'detached.py', 'language': 'python', 'source_only': True, 'content':
            "import os,pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)'],start_new_session=True)\n"
            "pathlib.Path(os.environ['LOCAL_CI_TASK_ROOT'],'child.pid').write_text(str(child.pid))\n"
            "time.sleep(90)\n"}
        result = []
        thread = threading.Thread(target=lambda: result.append(self.run_tool('custom', custom=script, cancelled=cancelled)))
        thread.start()
        pidfile = self.executor.host_root / 'candidate/child.pid'
        deadline = time.monotonic() + 20
        while not pidfile.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            self.assertTrue(pidfile.exists(), result)
            pid = int(pidfile.read_text())
        finally:
            cancelled.set()
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual('cancelled', result[0]['status'], result)
        process_stat = Path(f'/proc/{pid}/stat')
        self.assertTrue(not process_stat.exists() or process_stat.read_text().split()[2] == 'Z')
        self.assertFalse(self.executor.processes)

    def test_secret_profile_and_bad_parallelism_are_rejected(self):
        self.generation['env']['MODEL_API_KEY'] = 'private'
        record = self.run_tool()
        self.assertEqual('infra_error', record['status'])
        self.assertIn('Credentials', record['reason'])
        del self.generation['env']['MODEL_API_KEY']
        record = self.run_tool(parameters={'max_jobs': 100})
        self.assertIn('Parallelism', record['reason'])

    def test_private_service_umask_keeps_task_mount_traversable(self):
        task = {**self.task, 'task_id': 'd' * 64}
        previous = os.umask(0o077)
        try:
            self.executor = DockerExecutor(self.config, self.root / 'private-state', self.generation, task, LocalRelay(self.source))
            self.task = task
            result = self.run_tool('compile_time', 'base')
            candidate = self.run_tool('compile_time')
        finally:
            os.umask(previous)
        self.assertEqual('pass', result['status'], result)
        self.assertEqual('pass', candidate['status'], candidate)
        for relative in ('.', 'artifacts', '.trusted', '.trusted/baselines'):
            self.assertEqual(0o711, (self.executor.host_root / relative).stat().st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
