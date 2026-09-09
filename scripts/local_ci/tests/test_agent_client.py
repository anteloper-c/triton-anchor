"""Actual loopback broker/client HTTP; no Docker execution or model/API calls."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control.runtime.broker import Broker
from control.runtime.common import write_json
from control.runtime.engine import Engine


class AgentParametersFile(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.broker = Broker({}, {}, {}, self.root / 'output', lambda: False)
        self.broker.invoke = mock.Mock(return_value={'status': 'passed', 'summary': '中文回执'})
        port = self.broker.start(bind='127.0.0.1')
        self.addCleanup(self.broker.stop)
        self.environment = {**os.environ, 'LOCAL_CI_BROKER_URL': f'http://127.0.0.1:{port}/',
                            'LOCAL_CI_BROKER_TOKEN': self.broker.token, 'PYTHONIOENCODING': 'utf-8',
                            'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}
        self.client = Path(__file__).resolve().parents[1] / 'control/runtime/client.py'

    def send_file(self, raw, *options):
        path = self.root / '中文 review " file.json' if os.name != 'nt' else self.root / '中文 review file.json'
        path.write_bytes(raw)
        return subprocess.run([sys.executable, str(self.client), 'echo', '--parameters-file', str(path), *options],
                              capture_output=True, text=True, encoding='utf-8', env=self.environment, timeout=15,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_utf8_quotes_newlines_and_backslashes_arrive_without_shell_quoting(self):
        parameters = {'review': {'summary': '中文审查 "双引号" 与 \'单引号\'\n第二行 \\path\\',
                                  'findings': ['边界条件', '正常']}}
        result = self.send_file(json.dumps(parameters, ensure_ascii=False).encode('utf-8'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.broker.invoke.assert_called_once_with('echo', parameters)
        self.assertEqual(json.loads(result.stdout)['summary'], '中文回执')

    def test_exact_wire_limit_is_accepted_and_next_byte_is_rejected_before_http(self):
        overhead = len(json.dumps({'tool': 'echo', 'parameters': {'text': ''}}).encode())
        for delta in (0, 1):
            with self.subTest(delta=delta):
                self.broker.invoke.reset_mock()
                parameters = {'text': 'x' * (256000 - overhead + delta)}
                result = self.send_file(json.dumps(parameters).encode())
                self.assertEqual(result.returncode, 0 if delta == 0 else 2)
                self.assertEqual(self.broker.invoke.call_count, 1 if delta == 0 else 0)

    def test_raw_file_limit_and_json_expansion_are_bounded(self):
        for raw in (b' ' * 256001, json.dumps({'text': '中' * 43000}, ensure_ascii=False).encode()):
            with self.subTest(raw_bytes=len(raw)):
                self.broker.invoke.reset_mock()
                result = self.send_file(raw)
                self.assertEqual(result.returncode, 2)
                self.assertIn('limit', result.stderr)
                self.broker.invoke.assert_not_called()

    def test_invalid_json_and_mutually_exclusive_input_never_reach_broker(self):
        for raw, options in ((b'{"summary": "unterminated', ()), (b'{}', ('--parameters', '{}'))):
            with self.subTest(options=options):
                result = self.send_file(raw, *options)
                self.assertNotEqual(result.returncode, 0)
                self.broker.invoke.assert_not_called()

    def test_broker_error_is_preserved_as_nonzero_exit(self):
        self.broker.invoke.side_effect = ValueError('review validation rejected')
        result = self.send_file(b'{}')
        self.assertEqual(result.returncode, 2)
        self.assertIn('review validation rejected', json.loads(result.stdout)['reason'])


class PublicationCodexOptions(unittest.TestCase):
    def test_publication_resume_keeps_spark_high_and_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, output = root / 'workspace', root / 'output'
            agent = workspace / 'tasks/current/agent'
            agent.mkdir(parents=True)
            output.mkdir()
            settings = {'model': 'gpt-5.3-codex-spark', 'reasoning_effort': 'high',
                        'auto_compact_token_limit': 80000, 'tool_output_token_limit': 4000,
                        'publication_recovery_timeout': 90}
            profile = {'id': 'fixture', 'container': {'name': 'fixture-only'}}
            config = {'state_dir': str(root / 'state'), 'workspace_host': str(workspace),
                      'local_acceptance': True, 'profiles': [profile], 'codex': settings}
            manager = mock.Mock()
            engine = Engine(config, mock.Mock(), manager)
            record = output / 'execution.json'
            write_json(record, {'host_task': str(agent.parent), 'profile_id': 'fixture'})
            write_json(output / 'task.json', {'task_id': 'fixture-only'})
            session = '01a08034-d35e-7651-8a4d-d0a9bc8e6bc1'
            (output / 'agent-events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': session}) + '\n')
            with mock.patch('control.runtime.engine.execute') as execute, \
                 mock.patch('control.runtime.control.verify_control', return_value={}), \
                 mock.patch('control.runtime.control.verify_container_control'), mock.patch.object(engine, 'docker_run'):
                engine.recover_publication(record, 'local transport fixture')
            spec = json.loads(base64.urlsafe_b64decode(execute.call_args.args[0][-1]))
            self.assertEqual(spec['argv'][:3], ['codex', 'exec', 'resume'])
            self.assertIn(session, spec['argv'])
            for option in ('gpt-5.3-codex-spark', 'model_reasoning_effort="high"',
                           'model_auto_compact_token_limit=80000', 'tool_output_token_limit=4000'):
                self.assertIn(option, spec['argv'])
            self.assertEqual(execute.call_args.kwargs['timeout'], 90)
            manager.acquire.assert_called_once()
            manager.release.assert_called_once()

    def test_invalid_budget_values_fail_before_cli(self):
        for key in ('auto_compact_token_limit', 'tool_output_token_limit'):
            for value in (0, -1, True, '4000', 4.5):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'positive integer'):
                    Engine.codex_options({key: value})


if __name__ == '__main__':
    unittest.main()
