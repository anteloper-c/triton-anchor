"""Mock only Codex subprocess boundaries; no model or credential network calls."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.codex import CodexDriver, DISABLED_FEATURES, finish_timeout_seconds
from agent_ci.protocol import ContractError
from agent_ci.skill import load_skill, SkillBundle


SESSION_ID = '01234567-1234-1234-1234-0123456789ab'


class FakeProcess:
    def __init__(self, command, *, stdout, **kwargs):
        self.command, self.options = command, kwargs
        self.pid, self.returncode, self.polls = 432100, None, 0
        self.input_payload = b''
        process = self

        class Pipe(io.BytesIO):
            def close(self):
                process.input_payload = self.getvalue()
                super().close()

        self.stdin = Pipe()
        stdout.write((json.dumps({'type': 'thread.started', 'thread_id': SESSION_ID}) + '\n').encode())
        stdout.flush()

    def poll(self):
        self.polls += 1
        if self.polls >= 3:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux account boundary')
class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='local-ci-codex-')
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.source = self.root / 'company'
        self.source.mkdir()
        self.source.joinpath('config.toml').write_text('''model = "deployed-real-model"
model_provider = "company"
model_reasoning_effort = "high"
notify = ["do-not-run-personal-hook"]
[model_providers.company]
name = "Company"
base_url = "https://company.invalid/v1"
wire_api = "responses"
requires_openai_auth = true
[mcp_servers.personal]
command = "do-not-run-personal-server"
[features]
shell_tool = true
hooks = true
''')
        self.source.joinpath('auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'fixture-private-model-key'}))
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.account = types.SimpleNamespace(pw_uid=60001, pw_gid=60001, pw_name='fixture-codex', pw_dir='/nonexistent')
        self.supervisor = types.SimpleNamespace(task={'task_id': 'a' * 64}, executor=types.SimpleNamespace(uid=60002),
                                                run_dir=self.run_dir, cancelled=threading.Event(), closed=False)
        self.service = types.SimpleNamespace(path=self.root / 'task.sock', token='fixture-private-rpc-capability')
        self.config = {'codex_home': str(self.source), 'codex_sessions_root': str(self.root / 'sessions'),
                       'codex_user': self.account.pw_name, 'codex_bin': '/trusted/codex', 'python_bin': '/trusted/python'}
        self.driver = CodexDriver(self.config, self.root / 'state')
        self.processes = []

    def tearDown(self):
        self.temp.cleanup()

    def process(self, command, **kwargs):
        process = FakeProcess(command, **kwargs)
        self.processes.append(process)
        return process

    def run_driver(self, **kwargs):
        feature_output = '\n'.join(name + ' stable true' for name in DISABLED_FEATURES)
        with mock.patch.object(CodexDriver, 'account', return_value=self.account), \
             mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, feature_output, '')), \
             mock.patch('agent_ci.codex.subprocess.Popen', side_effect=self.process):
            return self.driver.run(self.supervisor, self.service, **kwargs)

    def test_new_and_resume_keep_provider_and_mcp_boundary(self):
        first = self.run_driver()
        command = self.processes[0].command
        self.assertIn('--sandbox', command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertNotIn(self.service.token, ' '.join(command))
        self.assertNotIn('fixture-private-model-key', ' '.join(command))
        env = self.processes[0].options['env']
        self.assertEqual(self.service.token, env['LOCAL_CI_RPC_TOKEN'])
        self.assertNotIn('OPENAI_API_KEY', env)
        config = Path(env['CODEX_HOME'], 'config.toml').read_text()
        self.assertIn('deployed-real-model', config)
        self.assertIn('https://company.invalid/v1', config)
        self.assertNotIn('do-not-run-personal', config)
        self.assertNotIn(self.service.token, config)
        self.assertIn('"env_vars" = ["LOCAL_CI_RPC_SOCKET", "LOCAL_CI_RPC_TOKEN", "LOCAL_CI_FINISH_TIMEOUT_SECONDS"]', config)
        self.assertEqual('3240', env['LOCAL_CI_FINISH_TIMEOUT_SECONDS'])
        self.assertIn('"tool_timeout_sec" = 3270', config)
        self.assertIn('"required" = true', config)
        self.assertIn('"shell_tool" = false', config)
        self.assertIn('"hooks" = false', config)
        self.assertIn('"multi_agent" = false', config)
        bundle = load_skill()
        self.assertTrue(self.processes[0].input_payload.decode().startswith(bundle.prompt))
        snapshot = Path(self.processes[0].options['cwd']) / 'TASK_SKILL.md'
        self.assertEqual(bundle.prompt, snapshot.read_text())
        self.assertEqual(0o444, snapshot.stat().st_mode & 0o777)
        manifest = json.loads((self.run_dir / 'skill-manifest.json').read_text())
        self.assertEqual(bundle.manifest['digest'], manifest['digest'])
        self.assertEqual(self.supervisor.task['task_id'], manifest['task_id'])
        self.assertIn('context', self.processes[0].input_payload.decode())
        self.assertEqual(SESSION_ID, first['session_id'])
        (self.run_dir / 'codex-events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': 'ffffffff-1234-1234-1234-0123456789ab'}) + '\n')
        second = self.run_driver(recovery='Continue saved evidence')
        command = self.processes[1].command
        self.assertEqual(['exec', 'resume', SESSION_ID], command[command.index('exec'):command.index('exec') + 3])
        self.assertNotIn('--sandbox', command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertEqual(SESSION_ID, second['session_id'])
        self.assertNotEqual(first['event_log'], second['event_log'])
        self.assertEqual(first['skill_digest'], second['skill_digest'])
        self.assertTrue(self.processes[1].input_payload.decode().startswith(bundle.prompt))

    def test_finish_deadline_covers_configured_hygiene_checks(self):
        config = {"hygiene_snapshot_timeout_seconds": 300, "cleanup_timeout_seconds": 40,
                  "profiles": {"3.0": {"post_task_validation_timeout_seconds": 180}}}
        self.assertEqual(2060, finish_timeout_seconds(config))
        with self.assertRaises(ContractError):
            finish_timeout_seconds({"hygiene_snapshot_timeout_seconds": True})

    def test_missing_skill_stops_before_codex_launch(self):
        with mock.patch('agent_ci.codex.load_skill', side_effect=ContractError('Missing Skill reference')):
            with self.assertRaisesRegex(ContractError, 'Missing Skill'):
                self.run_driver()
        self.assertFalse(self.processes)

    def test_resume_rejects_different_skill_or_flat_prompt_session(self):
        self.run_driver()
        path = self.run_dir / 'codex-session.json'
        original = json.loads(path.read_text())
        before_manifest = (self.run_dir / 'skill-manifest.json').read_bytes()
        bundle = load_skill()
        changed = SkillBundle(bundle.prompt + '\nNew rule', {**bundle.manifest, 'digest': '0' * 64})
        with mock.patch('agent_ci.codex.load_skill', return_value=changed):
            with self.assertRaisesRegex(ContractError, 'task/provider/Skill'):
                self.run_driver()
        self.assertEqual(before_manifest, (self.run_dir / 'skill-manifest.json').read_bytes())
        original.pop('skill_digest')
        path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ContractError, 'task/provider/Skill'):
            self.run_driver()
        self.assertEqual(1, len(self.processes))

    def test_session_is_saved_while_codex_is_still_running(self):
        observed = []
        def wait(seconds):
            observed.append(json.loads((self.run_dir / 'codex-session.json').read_text()))
            return False
        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(side_effect=wait))
        self.run_driver()
        self.assertTrue(observed)
        self.assertEqual(SESSION_ID, observed[0]['session_id'])
        self.assertEqual(self.supervisor.task['task_id'], observed[0]['task_id'])

    def test_saved_session_cannot_cross_task_or_provider(self):
        self.run_driver()
        path = self.run_dir / 'codex-session.json'
        saved = json.loads(path.read_text())
        saved['task_id'] = 'b' * 64
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ContractError, 'session identity'):
            self.run_driver()
        self.assertEqual(1, len(self.processes))

    def test_sealed_task_never_launches_or_resumes_codex(self):
        self.supervisor.closed = True
        result = self.run_driver()
        self.assertTrue(result['finished'])
        self.assertEqual('sealed', result['reason'])
        self.assertFalse(self.processes)

    def test_sealing_ends_current_codex_process(self):
        def seal(seconds):
            self.supervisor.closed = True
            return False
        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(side_effect=seal))
        with mock.patch.object(FakeProcess, 'poll', lambda process: process.returncode), mock.patch('agent_ci.codex.os.killpg'):
            result = self.run_driver()
        self.assertTrue(result['finished'])
        self.assertEqual('sealed', result['reason'])
        self.assertEqual(SESSION_ID, result['session_id'])

    def test_cancel_stops_process_and_retains_session(self):
        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(return_value=True))
        # Keep the process alive until the driver's cancellation path waits it out.
        def alive(process):
            return process.returncode
        with mock.patch.object(FakeProcess, 'poll', alive), mock.patch('agent_ci.codex.os.killpg') as kill:
            result = self.run_driver()
        self.assertEqual('cancelled', result['reason'])
        self.assertEqual(-15, result['exit_code'])
        self.assertEqual(SESSION_ID, result['session_id'])
        kill.assert_called_once()

    def test_same_container_uid_and_symlinked_credentials_rejected(self):
        self.supervisor.executor.uid = self.account.pw_uid
        with self.assertRaisesRegex(ContractError, 'different UIDs'):
            self.run_driver()
        self.supervisor.executor.uid = 60002
        auth = self.source / 'auth.json'
        auth.rename(self.source / 'auth-real.json')
        auth.symlink_to(self.source / 'auth-real.json')
        with self.assertRaisesRegex(ContractError, 'config.toml/auth.json'):
            self.run_driver()

    def test_missing_cli_capability_fails_before_model(self):
        with mock.patch.object(CodexDriver, 'account', return_value=self.account), \
             mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'shell_tool stable true', '')), \
             mock.patch('agent_ci.codex.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ContractError, 'tool boundary'):
                self.driver.run(self.supervisor, self.service)
            popen.assert_not_called()

    def test_administrative_account_rejected(self):
        group = types.SimpleNamespace(gr_name='docker', gr_gid=555, gr_mem=[self.account.pw_name])
        with mock.patch('agent_ci.codex.pwd.getpwnam', return_value=self.account), \
             mock.patch('agent_ci.codex.grp.getgrall', return_value=[group]):
            with self.assertRaisesRegex(ContractError, 'administrative'):
                CodexDriver.account(self.account.pw_name)


if __name__ == '__main__':
    unittest.main()
