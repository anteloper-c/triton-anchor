"""Mock only Codex subprocess boundaries; no model or credential network calls."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.codex import CodexDriver, DISABLED_FEATURES, ENABLED_FEATURES, finish_timeout_seconds
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


class ContainerExecutor:
    """Only container transport is mocked; inspect the private delivery boundary."""
    def __init__(self):
        self.generation = {'attempt_id': 'fixture-attempt-1',
                           'uids': {'codex': 60001, 'candidate': 60002, 'base': 60003, 'diagnostic': 60004}}
        self.layout = {'home': '/codex/home', 'workspace': '/codex/workspace',
                       'python_bin': '/usr/bin/python3', 'mcp_script': '/trusted/agent_ci/mcp_server.py',
                       'rpc_socket': '/run/local-ci-rpc/supervisor.sock'}
        self.files, self.environment, self.deliveries, self.commands = {}, {}, [], []
        self.stops = 0
        self.exports = []

    def prepare_native_workspace(self):
        return {'root': '/codex/workspace/candidate', 'checkout': '/codex/workspace/candidate/checkout'}

    def native_environment(self, layout):
        return {'PATH': layout['root'] + '/venv/bin:/usr/bin:/bin', 'HOME': layout['root'] + '/home'}

    def export_native_evidence(self, destination):
        self.exports.append(destination)
        destination.mkdir()
        (destination / 'manifest.json').write_text('{}')
        return {'exported': True}

    def prepare_codex_session(self, *, files, environment, rpc_socket):
        self.deliveries.append({'files': dict(files), 'environment': dict(environment), 'rpc_socket': rpc_socket})
        self.files.update(files)
        self.environment.update(environment)
        return self.layout

    def codex_command(self, arguments):
        command = ['docker', '--host', 'unix:///run/user/1000/docker.sock', 'exec', '-i', '--user', '60001:60001',
                   'owned-task-container', '/trusted/private-env-launcher', '/trusted/codex', *arguments]
        self.commands.append(command)
        return command

    def stop_codex(self):
        self.stops += 1
        return {'verified': True, 'remaining': [], 'cleaned_pid_count': 0}


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux container client boundary')
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
        self.executor = ContainerExecutor()
        self.supervisor = types.SimpleNamespace(task={'task_id': 'a' * 64}, executor=self.executor,
                                                run_dir=self.run_dir, cancelled=threading.Event(), closed=False)
        self.service = types.SimpleNamespace(path=self.root / 'task.sock', token='fixture-private-rpc-capability')
        self.config = {'codex_home': str(self.source), 'codex_bin': '/trusted/codex'}
        self.driver = CodexDriver(self.config, self.root / 'state')
        self.processes = []

    def tearDown(self):
        self.temp.cleanup()

    def process(self, command, **kwargs):
        process = FakeProcess(command, **kwargs)
        self.processes.append(process)
        return process

    def run_driver(self, **kwargs):
        feature_output = '\n'.join(name + ' stable true' for name in DISABLED_FEATURES | ENABLED_FEATURES)
        with mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, feature_output, '')), \
             mock.patch('agent_ci.codex.subprocess.Popen', side_effect=self.process):
            return self.driver.run(self.supervisor, self.service, **kwargs)

    def test_new_and_resume_keep_provider_and_mcp_boundary(self):
        first = self.run_driver()
        command = self.processes[0].command
        self.assertIn('--sandbox', command)
        self.assertIn('sandbox_mode="danger-full-access"', command)
        self.assertEqual('danger-full-access', command[command.index('--sandbox') + 1])
        self.assertEqual('/codex/workspace/candidate/checkout', command[command.index('--cd') + 1])
        self.assertIn('approval_policy="never"', command)
        self.assertIn('features.shell_tool=true', command)
        self.assertIn('features.unified_exec=true', command)
        self.assertNotIn(self.service.token, ' '.join(command))
        self.assertNotIn('fixture-private-model-key', ' '.join(command))
        client_env = self.processes[0].options['env']
        self.assertNotIn('LOCAL_CI_RPC_TOKEN', client_env)
        self.assertNotIn('OPENAI_API_KEY', client_env)
        env = self.executor.environment
        self.assertEqual(self.service.token, env['LOCAL_CI_RPC_TOKEN'])
        self.assertEqual('/run/local-ci-rpc/supervisor.sock', env['LOCAL_CI_RPC_SOCKET'])
        self.assertNotIn('OPENAI_API_KEY', env)
        config = self.executor.files['config.toml']
        self.assertIn('deployed-real-model', config)
        self.assertIn('https://company.invalid/v1', config)
        self.assertNotIn('do-not-run-personal', config)
        self.assertNotIn(self.service.token, config)
        self.assertIn('"env_vars" = ["LOCAL_CI_RPC_SOCKET", "LOCAL_CI_RPC_TOKEN", "LOCAL_CI_FINISH_TIMEOUT_SECONDS"]', config)
        self.assertEqual('3600', env['LOCAL_CI_FINISH_TIMEOUT_SECONDS'])
        self.assertIn('"tool_timeout_sec" = 3630', config)
        self.assertIn('"required" = true', config)
        self.assertIn('"shell_tool" = true', config)
        self.assertIn('"unified_exec" = true', config)
        self.assertIn('"apply_patch_freeform" = true', config)
        self.assertIn('"hooks" = false', config)
        self.assertIn('"multi_agent" = false', config)
        self.assertIn('/trusted/agent_ci/mcp_server.py', config)
        self.assertNotIn('runuser', command)
        bundle = load_skill()
        self.assertTrue(self.processes[0].input_payload.decode().startswith(bundle.prompt))
        self.assertEqual(bundle.prompt, self.executor.files['TASK_SKILL.md'])
        self.assertEqual(self.run_dir, self.processes[0].options['cwd'])
        self.assertEqual(self.service.path, self.executor.deliveries[0]['rpc_socket'])
        manifest = json.loads((self.run_dir / 'skill-manifest.json').read_text())
        self.assertEqual(bundle.manifest['digest'], manifest['digest'])
        self.assertEqual(self.supervisor.task['task_id'], manifest['task_id'])
        self.assertIn('context', self.processes[0].input_payload.decode())
        self.assertEqual(SESSION_ID, first['session_id'])
        (self.run_dir / 'codex-events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': 'ffffffff-1234-1234-1234-0123456789ab'}) + '\n')
        second = self.run_driver(recovery='Continue saved evidence')
        command = self.processes[1].command
        resume = command.index('resume')
        self.assertEqual(['exec', 'resume', SESSION_ID], command[resume - 1:resume + 2])
        self.assertNotIn('--sandbox', command)
        self.assertIn('sandbox_mode="danger-full-access"', command)
        self.assertIn('features.shell_tool=true', command)
        self.assertIn('features.unified_exec=true', command)
        self.assertIn('approval_policy="never"', command)
        self.assertEqual(SESSION_ID, second['session_id'])
        self.assertNotEqual(first['event_log'], second['event_log'])
        self.assertEqual(first['skill_digest'], second['skill_digest'])
        self.assertTrue(self.processes[1].input_payload.decode().startswith(bundle.prompt))
        self.assertEqual(4, self.executor.stops)
        self.assertEqual(2, len(self.executor.exports))
        self.assertTrue(Path(first['native_evidence']).is_dir())
        self.assertNotEqual(first['native_evidence'], second['native_evidence'])

    def test_native_command_and_edit_events_are_private_exploration_records(self):
        def native_process(command, **kwargs):
            process = self.process(command, **kwargs)
            events = [
                {'type': 'item.started', 'item': {'id': 'cmd', 'type': 'command_execution',
                    'command': 'python repro.py', 'status': 'in_progress'}},
                {'type': 'item.completed', 'item': {'id': 'cmd', 'type': 'command_execution',
                    'command': 'python repro.py', 'status': 'failed', 'exit_code': 3,
                    'aggregated_output': 'fixture-private-model-key ' + self.service.token}},
                {'type': 'item.completed', 'item': {'id': 'edit', 'type': 'file_change',
                    'status': 'completed', 'changes': [{'path': 'repro.py', 'kind': 'add'}]}},
            ]
            for event in events:
                kwargs['stdout'].write((json.dumps(event) + '\n').encode())
            kwargs['stdout'].flush()
            return process
        features = '\n'.join(name + ' stable true' for name in DISABLED_FEATURES | ENABLED_FEATURES)
        with mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, features, '')), \
             mock.patch('agent_ci.codex.subprocess.Popen', side_effect=native_process):
            result = self.driver.run(self.supervisor, self.service)
        path = Path(result['native_audit'])
        actions = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(3, len(actions))
        self.assertEqual(3, actions[1]['exit_code'])
        self.assertTrue(all(not action['counts_as_check'] for action in actions))
        self.assertTrue(all(action['task_id'] == self.supervisor.task['task_id'] for action in actions))
        self.assertNotIn(self.service.token, path.read_text())
        self.assertNotIn('fixture-private-model-key', path.read_text())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o600, Path(result['event_log']).stat().st_mode & 0o777)
        self.assertFalse((self.run_dir / 'published').exists())

    def test_native_export_failure_does_not_erase_event_history(self):
        with mock.patch.object(self.executor, 'export_native_evidence', side_effect=ContractError('native evidence export failed')):
            with self.assertRaisesRegex(ContractError, 'native evidence export failed'):
                self.run_driver()
        self.assertEqual(SESSION_ID, json.loads((self.run_dir / 'codex-session.json').read_text())['session_id'])
        self.assertTrue(list(self.run_dir.glob('codex-events-*.jsonl')))

    def test_finish_budget_uses_cleanup_and_management_contract(self):
        self.assertEqual(3600, finish_timeout_seconds({}))
        config = {"finish_timeout_seconds": 1900, "cleanup_timeout_seconds": 100,
                  "management_timeout_seconds": 1500,
                  "hygiene_snapshot_timeout_seconds": True,
                  "profiles": {"3.0": {"post_task_validation_timeout_seconds": "obsolete"}}}
        self.assertEqual(1900, finish_timeout_seconds(config))
        self.config.update(config)
        self.run_driver()
        self.assertEqual('1900', self.executor.environment['LOCAL_CI_FINISH_TIMEOUT_SECONDS'])
        self.assertIn('"tool_timeout_sec" = 1930', self.executor.files['config.toml'])

    def test_finish_budget_rejects_invalid_or_insufficient_limits(self):
        for config in ({"finish_timeout_seconds": True}, {"finish_timeout_seconds": 86301},
                       {"finish_timeout_seconds": 0}, {"finish_timeout_seconds": 839},
                       {"cleanup_timeout_seconds": True}, {"management_timeout_seconds": 0},
                       {"cleanup_timeout_seconds": 1200}, {"management_timeout_seconds": "600"}):
            with self.subTest(config=config), self.assertRaises(ContractError):
                finish_timeout_seconds(config)
        self.assertEqual(840, finish_timeout_seconds({"finish_timeout_seconds": 840}))
        self.assertEqual(86300, finish_timeout_seconds({"finish_timeout_seconds": 86300}))

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

    def test_replaced_attempt_archives_old_session_and_starts_new(self):
        self.run_driver()
        previous = json.loads((self.run_dir / 'codex-session.json').read_text())
        self.executor.generation['attempt_id'] = 'fixture-attempt-2'
        self.run_driver()
        self.assertNotIn('resume', self.processes[1].command)
        self.assertIn('previous task container/session volume was replaced', self.processes[1].input_payload.decode())
        history = list((self.run_dir / 'codex-session-history').glob('*.json'))
        self.assertEqual(1, len(history))
        archived = json.loads(history[0].read_text())
        self.assertEqual(previous['session_id'], archived['session_id'])
        self.assertEqual('fixture-attempt-1', archived['attempt_id'])
        self.assertEqual('fixture-attempt-2', archived['replacement_attempt_id'])
        self.assertEqual('fixture-attempt-2', json.loads((self.run_dir / 'codex-session.json').read_text())['attempt_id'])
        self.run_driver()
        self.assertIn('resume', self.processes[2].command)

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

    def test_active_seal_has_its_own_budget_after_model_deadline(self):
        self.config['codex_timeout_seconds'] = 10
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.supervisor.sealing_started = True
            if len(waits) == 2:
                self.supervisor.closed = True
            return False

        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(side_effect=wait))
        with mock.patch.object(FakeProcess, 'poll', lambda process: process.returncode), \
             mock.patch('agent_ci.codex.os.killpg'), \
             mock.patch('agent_ci.codex.time.monotonic', side_effect=[0, 11, 200, 201]):
            result = self.run_driver()
        self.assertEqual('sealed', result['reason'])
        self.assertTrue(result['finished'])
        self.assertEqual(2, len(waits))

    def test_active_seal_budget_is_bounded_and_not_reset_each_poll(self):
        self.config.update(codex_timeout_seconds=10, finish_timeout_seconds=840)

        def wait(seconds):
            self.supervisor.sealing_started = True
            return False

        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(side_effect=wait))
        with mock.patch.object(FakeProcess, 'poll', lambda process: process.returncode), \
             mock.patch('agent_ci.codex.os.killpg'), \
             mock.patch('agent_ci.codex.time.monotonic', side_effect=[0, 11, 900, 901]):
            result = self.run_driver()
        self.assertEqual('timeout', result['reason'])
        self.assertFalse(result['finished'])
        self.assertEqual(2, self.supervisor.cancelled.wait.call_count)
        self.assertEqual(2, self.executor.stops)

    def test_model_deadline_still_applies_before_sealing(self):
        self.config['codex_timeout_seconds'] = 10
        self.supervisor.cancelled = mock.Mock(is_set=mock.Mock(return_value=False), wait=mock.Mock(return_value=False))
        with mock.patch.object(FakeProcess, 'poll', lambda process: process.returncode), \
             mock.patch('agent_ci.codex.os.killpg'), \
             mock.patch('agent_ci.codex.time.monotonic', side_effect=[0, 11, 12]):
            result = self.run_driver()
        self.assertEqual('timeout', result['reason'])
        self.assertEqual(1, self.supervisor.cancelled.wait.call_count)

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
        self.executor.generation['uids']['candidate'] = 60001
        with self.assertRaisesRegex(ContractError, 'different non-root UIDs'):
            self.run_driver()
        self.executor.generation['uids']['candidate'] = 60002
        auth = self.source / 'auth.json'
        auth.rename(self.source / 'auth-real.json')
        auth.symlink_to(self.source / 'auth-real.json')
        with self.assertRaisesRegex(ContractError, 'config.toml/auth.json'):
            self.run_driver()

    def test_missing_cli_capability_fails_before_model(self):
        with mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'shell_tool stable true', '')), \
             mock.patch('agent_ci.codex.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ContractError, 'tool boundary'):
                self.driver.run(self.supervisor, self.service)
            popen.assert_not_called()
        self.assertEqual(1, self.executor.stops)

    def test_removed_editing_flag_is_not_reenabled(self):
        features = '\n'.join(name + ' stable true' for name in DISABLED_FEATURES | ENABLED_FEATURES
                             if name != 'apply_patch_freeform') + '\napply_patch_freeform removed false'
        with mock.patch('agent_ci.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 0, features, '')), \
             mock.patch('agent_ci.codex.subprocess.Popen', side_effect=self.process):
            self.driver.run(self.supervisor, self.service)
        config = self.executor.files['config.toml']
        self.assertNotIn('apply_patch_freeform', config)
        self.assertIn('"multi_agent_v2" = false', config)
        self.assertIn('"shell_tool" = true', config)

    def test_feature_probe_timeout_still_reaps_container_codex_uid(self):
        with mock.patch('agent_ci.codex.subprocess.run', side_effect=subprocess.TimeoutExpired(['codex', 'features', 'list'], 30)), \
             mock.patch('agent_ci.codex.subprocess.Popen') as popen:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.driver.run(self.supervisor, self.service)
            popen.assert_not_called()
        self.assertEqual(1, self.executor.stops)

    def test_unprivileged_host_never_chowns_or_launches_runuser(self):
        with mock.patch('os.geteuid', return_value=1000), mock.patch('os.chown', side_effect=AssertionError('host chown')):
            self.run_driver()
        self.assertNotIn('runuser', self.processes[0].command)
        self.assertEqual(2, self.executor.stops)

    def test_company_env_credentials_are_private_and_unrelated_secrets_not_inherited(self):
        path = self.source / 'config.toml'
        path.write_text(path.read_text().replace('name = "Company"', 'name = "Company"\nenv_key = "COMPANY_TOKEN"'))
        with mock.patch.dict(os.environ, {'COMPANY_TOKEN': 'fixture-env-secret', 'GITEE_TOKEN': 'never-to-model',
                                        'LOCAL_CI_RPC_TOKEN': 'other-task-token'}):
            self.run_driver()
        self.assertEqual('fixture-env-secret', self.executor.environment['COMPANY_TOKEN'])
        self.assertNotIn('GITEE_TOKEN', self.executor.environment)
        for command in self.executor.commands:
            self.assertNotIn('fixture-env-secret', ' '.join(command))
            self.assertNotIn(self.service.token, ' '.join(command))
        self.assertNotIn('COMPANY_TOKEN', self.processes[0].options['env'])

    def test_cleanup_failure_propagates_after_retaining_events(self):
        with mock.patch.object(self.executor, 'stop_codex', side_effect=[{}, ContractError('Codex process cleanup failed')]):
            with self.assertRaisesRegex(ContractError, 'cleanup failed'):
                self.run_driver()
        self.assertEqual(SESSION_ID, json.loads((self.run_dir / 'codex-session.json').read_text())['session_id'])


if __name__ == '__main__':
    unittest.main()
