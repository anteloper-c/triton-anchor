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
from agent_ci.executor import DockerExecutor, ProcessCleanupError, STOP_PROGRAM, VENV_PROGRAM
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
prefix=[]; nested=args
if len(args)>5 and args[1:4]==['-I','-S','-c'] and 'PR_SET_NO_NEW_PRIVS' in args[4]:
    prefix=args[:5];nested=args[5:]
if len(nested)>3 and nested[1]=='-c' and 'marker=target/' in nested[2]:
    assert '--system-site-packages' not in nested[2] and '--copies' in nested[2]
    # The expensive seeded package copy is the fake boundary. venv creation is real.
    args=prefix+[nested[0],'-c',"import json,pathlib,subprocess,sys; p=pathlib.Path(sys.argv[1]); subprocess.run([sys.executable,'-m','venv','--without-pip','--copies',str(p)],check=True) if not (p/'bin/python').exists() else None; (p/'.local-ci-environment.json').write_text(json.dumps({'fingerprint':sys.argv[2]}))",*nested[3:]]
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


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0, 'Linux root required for isolated test UIDs')
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
        # FakeDocker has no PID namespace, so never target a real host account.
        uid = 100000 + int(uuid.uuid4().hex[:8], 16) % 1000000
        gid = uid
        self.task = {'task_id': 'a' * 64, 'base_sha': base, 'tested_sha': candidate,
                     'llvm_hash': 'b' * 40, 'full': False, 'worker_revision_sha': 'c' * 40}
        self.generation = {'workspace_host': str(self.root / 'workspace'), 'workspace_container': str(self.root / 'workspace'),
                           'container': 'fake-persistent', 'profile': 'triton30', 'backend_enabled': False,
                           'generation': 'fixture-generation',
                           'execution_user': f'{uid}:{gid}', 'execution_uid': uid, 'execution_gid': gid,
                           'environment_fingerprint': 'environment-v1',
                           'env': {'SEED_PYTHON': sys.executable, 'PYTHON_VENV_ACTIVATE': '/profile/seed/bin/activate'}}
        self.config = {'simulation': True, 'docker_bin': str(self.fake), 'container_control_root': str(self.root / 'control')}
        self.executor = DockerExecutor(self.config, self.root / 'state', self.generation, self.task, LocalRelay(self.source))

    def tearDown(self):
        try:
            self.executor.runner = subprocess.run
            self.executor.stop_task()
        finally:
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
        self.assertEqual(self.generation['generation'], record['workspace_generation'])
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
            "child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)'],start_new_session=True,env={})\n"
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
            status = Path(f'/proc/{pid}/status').read_text()
            self.assertIn('NoNewPrivs:\t1', status)
            self.assertNotIn(b'LOCAL_CI_EXECUTION_ID', Path(f'/proc/{pid}/environ').read_bytes())
        finally:
            cancelled.set()
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual('cancelled', result[0]['status'], result)
        process_stat = Path(f'/proc/{pid}/stat')
        self.assertTrue(not process_stat.exists() or process_stat.read_text().split()[2] == 'Z')
        self.assertFalse(self.executor.processes)

    def detached(self, *, uid=None):
        process = subprocess.Popen([sys.executable, '-I', '-S', '-c',
            'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)'],
            env={}, user=self.executor.uid if uid is None else uid,
            group=self.executor.gid if uid is None else uid, extra_groups=(), start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        self.addCleanup(cleanup)
        time.sleep(.1)
        return process

    def test_task_cleanup_after_restart_reaps_markerless_uid_and_preserves_root(self):
        child = self.detached()
        root_management = self.detached(uid=0)
        restarted = DockerExecutor(self.config, self.root / 'state', self.generation, self.task, LocalRelay(self.source))
        self.assertFalse(restarted.processes)
        try:
            result = restarted.stop_task(self.task['task_id'])
            self.assertEqual([], result['remaining'])
            self.assertGreaterEqual(result['cleaned_pid_count'], 1)
            self.assertTrue(result['verified'])
            child.wait(timeout=2)
            self.assertIsNone(root_management.poll())
            with self.assertRaises(ContractError):
                restarted.stop_task('f' * 64)
        finally:
            root_management.kill()
            root_management.wait(timeout=2)

    def test_queued_stop_does_not_clean_other_active_uid_work(self):
        child = self.detached()
        self.assertEqual('not_active', self.executor.stop(uuid.uuid4().hex)['status'])
        self.assertIsNone(child.poll())
        self.executor.stop_task()
        child.wait(timeout=2)

    def test_successful_parent_cannot_leave_markerless_detached_child(self):
        custom = {'name': 'daemon.py', 'language': 'python', 'source_only': True, 'content':
            "import os,pathlib,subprocess,sys\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True,env={})\n"
            "pathlib.Path(os.environ['LOCAL_CI_TASK_ROOT'],'daemon.pid').write_text(str(child.pid))\n"}
        result = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', result['status'], result)
        pid = int((self.executor.host_root / 'candidate/daemon.pid').read_text())
        process_stat = Path(f'/proc/{pid}/stat')
        self.assertTrue(not process_stat.exists() or process_stat.read_text().split()[2] == 'Z')

    def test_cleanup_failure_is_infrastructure_failure_even_after_zero_exit(self):
        cases = (
            subprocess.CompletedProcess([], 1, b'{"error":"permission denied"}'),
            subprocess.CompletedProcess([], 0, b'not JSON'),
            subprocess.CompletedProcess([], 0, json.dumps({'schema': 'triton-anchor-process-cleanup/v1',
                'uid': self.executor.uid, 'status': 'clean', 'verified': True, 'remaining': [123], 'cleaned_pid_count': 0}).encode()),
        )
        for response in cases:
            with self.subTest(stdout=response.stdout):
                self.executor.runner = mock.Mock(return_value=response)
                result = self.run_tool()
                self.assertEqual('infra_error', result['status'], result)
                self.assertIn('cleanup', result['reason'].lower())
                self.assertFalse(self.executor.processes)

    def test_missing_pidfd_support_is_explicit_failure(self):
        command = [sys.executable, '-I', '-S', '-c',
                   'import os; del os.pidfd_open\n' + STOP_PROGRAM, str(self.executor.uid)]
        completed = subprocess.run(command, capture_output=True)
        self.assertNotEqual(0, completed.returncode)
        report = json.loads(completed.stdout)
        self.assertFalse(report['verified'])
        self.assertIn('pidfd support', report['error'])
        with mock.patch.object(self.executor, 'runner', return_value=completed):
            with self.assertRaises(ProcessCleanupError):
                self.executor.stop_task()

    def test_launcher_no_new_privileges_cannot_be_cleared(self):
        custom = {'name': 'privileges.py', 'language': 'python', 'source_only': True, 'content':
            "import ctypes\n"
            "libc=ctypes.CDLL(None,use_errno=True)\n"
            "assert libc.prctl(39,0,0,0,0)==1, 'no_new_privs was not set'\n"
            "assert libc.prctl(38,0,0,0,0)!=0, 'no_new_privs could be cleared'\n"
            "print('no_new_privs is irreversible')\n"}
        result = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', result['status'], result)
        self.assertIn('irreversible', (Path(result['artifact_dir']) / 'execution.log').read_text())

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
