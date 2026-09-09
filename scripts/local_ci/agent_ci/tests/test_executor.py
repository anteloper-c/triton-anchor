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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_ci.executor import DockerExecutor, ProcessCleanupError, STOP_PROGRAM, VENV_PROGRAM
from agent_ci.protocol import ContractError
from container_fixture import FAKE_DOCKER, VolumeManager


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
        self.task = {'task_id': 'a' * 64, 'base_sha': base, 'tested_sha': candidate,
                     'llvm_hash': 'b' * 40, 'full': False, 'worker_revision_sha': 'c' * 40, 'target_branch': 'main'}
        self.manager = VolumeManager(self.root, docker_bin=self.fake)
        self.generation = self.manager.acquire_task(self.task, 'simulation-run', rpc_directory=self.root / 'rpc')
        self.generation['env'] = {'SEED_PYTHON': sys.executable, 'PYTHON_VENV_ACTIVATE': '/profile/seed/bin/activate'}
        self.config = {'simulation': True, 'docker_bin': str(self.fake), 'container_control_root': str(self.root / 'control'),
                       'runtime': {'kind': 'docker-rootless', 'endpoint': 'unix:///run/user/1000/docker.sock'}}
        self.executor = DockerExecutor(self.config, self.root / 'state', self.generation, self.task,
                                       LocalRelay(self.source), manager=self.manager)

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
        root = self.manager.volume(self.generation)
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
            "pathlib.Path(os.environ['TMPDIR'],'child.pid').write_text(str(child.pid))\n"
            "time.sleep(90)\n"}
        result = []
        thread = threading.Thread(target=lambda: result.append(self.run_tool('custom', custom=script, cancelled=cancelled)))
        thread.start()
        volume = self.manager.volume(self.generation)
        deadline = time.monotonic() + 20
        matches = []
        while not matches and thread.is_alive() and time.monotonic() < deadline:
            matches = list(volume.glob('diagnostics/*/tmp/child.pid'))
            time.sleep(0.05)
        pidfile = matches[0] if matches else volume / 'missing.pid'
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
        restarted = DockerExecutor(self.config, self.root / 'state', self.generation, self.task, LocalRelay(self.source), manager=self.manager)
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
            "pathlib.Path(os.environ['TMPDIR'],'daemon.pid').write_text(str(child.pid))\n"}
        result = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', result['status'], result)
        pid = int(next(self.manager.volume(self.generation).glob('diagnostics/*/tmp/daemon.pid')).read_text())
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

    def test_diagnostic_reads_formal_state_but_cannot_write_it_or_read_codex_auth(self):
        self.assertEqual("pass", self.run_tool()["status"])
        self.assertEqual("pass", self.run_tool(variant="base")["status"])
        self.manager.deploy_session(self.generation, {"auth.json": "private-model-token"}, {})
        custom = {"name": "isolation.py", "language": "python", "source_only": True,
                  "content": "import os,pathlib\n"
                  "assert pathlib.Path(os.environ['ANCHOR_DIR'],'README.md').read_text()=='candidate\\n'\n"
                  "for path in ['/task/candidate/checkout/README.md','/task/base/checkout/README.md','/task/candidate/venv/injected.py']:\n"
                  " try: pathlib.Path(path).write_text('corrupt')\n"
                  " except PermissionError: pass\n"
                  " else: raise AssertionError('diagnostic wrote formal state: '+path)\n"
                  "try: pathlib.Path('/codex/home/auth.json').read_text()\n"
                  "except PermissionError: pass\n"
                  "else: raise AssertionError('model credentials readable')\n"
                  "pathlib.Path(os.environ['TMPDIR'],'scratch').write_text('allowed')\n"}
        record = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', record['status'], record)
        self.assertEqual(self.generation['uids']['diagnostic'], record['execution_uid'])

    def test_experiment_is_writable_and_resume_cannot_switch_variant(self):
        custom = {"name": "experiment.py", "language": "python", "mode": "experiment",
                  "content": "import os,pathlib\npathlib.Path(os.environ['ANCHOR_DIR'],'README.md').write_text('experiment')\n"}
        first = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', first['status'], first)
        ident = first['experiment_id']
        custom.update(experiment_id=ident, content="import os,pathlib; assert pathlib.Path(os.environ['ANCHOR_DIR'],'README.md').read_text()=='experiment'")
        self.assertEqual('pass', self.run_tool('custom', custom=custom)['status'])
        formal = self.manager.volume(self.generation) / 'candidate/checkout/README.md'
        self.assertEqual('candidate\n', formal.read_text())
        other = self.run_tool('custom', variant='base', custom=custom)
        self.assertEqual('infra_error', other['status'])
        self.assertIn('variant', other['reason'])

    def test_test_cleanup_preserves_codex_until_explicit_session_stop(self):
        codex = self.detached(uid=self.generation['uids']['codex'])
        children = [self.detached(uid=self.generation['uids'][role]) for role in ('candidate','base','diagnostic')]
        self.assertTrue(self.executor.stop_task()['verified'])
        for child in children:
            child.wait(timeout=2)
        self.assertIsNone(codex.poll())
        self.executor.stop_codex()
        codex.wait(timeout=2)

    def test_reproduction_loads_setup_but_preserves_task_paths(self):
        self.assertEqual('pass', self.run_tool()['status'])
        setup = self.root / 'trusted-setup.sh'
        setup.write_text('export LOADED_BACKEND=ready\nexport TMPDIR=/shared-tmp\nexport ANCHOR_DIR=/wrong-source\nexport PATH=/usr/bin:/bin\n')
        self.generation['env']['TRUSTED_ANCHOR_ENVSETUP'] = str(setup)
        custom = {'name':'environment.py', 'language':'python', 'mode':'reproduction',
                  'content':"import os; assert os.environ['LOADED_BACKEND']=='ready'; assert '/diagnostics/' in os.environ['TMPDIR']; assert os.environ['ANCHOR_DIR'].endswith('/candidate/checkout')"}
        record = self.run_tool('custom', custom=custom)
        self.assertEqual('pass', record['status'], record)
        self.assertEqual(2, len(record['environment_setup']))
        self.assertTrue(list(Path(record['artifact_dir']).glob('setup-*.log')))
        setup.write_text('return 9\n')
        failed = self.run_tool('custom', custom=custom)
        self.assertEqual('infra_error', failed['status'], failed)
        self.assertEqual('custom_environment_setup_failed', failed['reason'])
        custom.update(mode='diagnostic', source_only=True, content='print("can inspect broken setup")')
        self.assertEqual('pass', self.run_tool('custom', custom=custom)['status'])

    def test_verify_exception_still_exports_tool_artifacts(self):
        with mock.patch.object(self.manager, 'verify_checkout', side_effect=OSError('snapshot check interrupted')):
            result = self.run_tool('compile_time')
        self.assertEqual('infra_error', result['status'])
        self.assertTrue(result['evidence_exported'])
        self.assertTrue((Path(result['artifact_dir']) / 'candidate.json').is_file())

    def test_private_service_umask_keeps_task_mount_traversable(self):
        task = {**self.task, 'task_id': 'd' * 64}
        previous = os.umask(0o077)
        try:
            self.generation = self.manager.acquire_task(task, 'private-run', rpc_directory=self.root / 'private-rpc')
            self.generation['env'] = {'SEED_PYTHON': sys.executable}
            self.executor = DockerExecutor(self.config, self.root / 'private-state', self.generation, task, LocalRelay(self.source), manager=self.manager)
            self.task = task
            result = self.run_tool('compile_time', 'base')
            candidate = self.run_tool('compile_time')
        finally:
            os.umask(previous)
        self.assertEqual('pass', result['status'], result)
        self.assertEqual('pass', candidate['status'], candidate)
        self.assertEqual(0o700, (self.root / 'private-state').stat().st_mode & 0o777)
        self.assertNotEqual(self.executor.host_root, self.manager.volume(self.generation))


if __name__ == '__main__':
    unittest.main()
