"""Provider selection and credential boundaries; no model/API or Docker calls."""
import base64
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.broker import Broker
from runtime.common import digest, write_json
from runtime.engine import Engine


PROVIDER = {'id': 'deepseek', 'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
            'env_key': 'DEEPSEEK_API_KEY', 'wire_api': 'responses'}
SETTINGS = {'model': 'deepseek-v4-flash', 'reasoning_effort': 'high', 'provider': PROVIDER,
            'model_catalog_json': '/opt/anchor-ci/models/deepseek.json', 'timeout': 200}
SESSION = '01a08034-d35e-7651-8a4d-d0a9bc8e6bc1'


class ProviderConfiguration(unittest.TestCase):
    def test_native_responses_options_are_valid_toml_and_keep_credentials_out(self):
        options = Engine.codex_options(SETTINGS)
        config = tomllib.loads('\n'.join(options[index + 1] for index, value in enumerate(options) if value == '-c'))
        self.assertEqual(options[:2], ['-m', 'deepseek-v4-flash'])
        self.assertEqual(config['model_provider'], 'deepseek')
        self.assertEqual(config['model_providers']['deepseek'], {key: value for key, value in PROVIDER.items() if key != 'id'})
        self.assertEqual(config['model_catalog_json'], SETTINGS['model_catalog_json'])
        self.assertEqual(config['shell_environment_policy']['exclude'], ['DEEPSEEK_API_KEY'])
        self.assertNotIn('LOCAL_CI_BROKER_TOKEN', config['shell_environment_policy']['exclude'])
        self.assertFalse(config['allow_login_shell'])
        self.assertFalse(config['features']['shell_snapshot'])

    def test_optional_name_and_wire_defaults_and_builtin_provider_remain_compatible(self):
        provider = {key: value for key, value in PROVIDER.items() if key not in ('name', 'wire_api')}
        self.assertEqual(Engine.codex_provider({'provider': provider})['name'], 'deepseek')
        self.assertEqual(Engine.codex_provider({'provider': provider})['wire_api'], 'responses')
        self.assertIsNone(Engine.codex_provider({}))
        self.assertEqual(Engine.codex_options({'model': 'gpt-5.3-codex-spark'}), ['-m', 'gpt-5.3-codex-spark'])

    def test_invalid_provider_config_is_rejected_without_echoing_its_value(self):
        changes = [('id', 'deepseek.override'), ('id', ''), ('id', 4), ('name', ''), ('name', 'bad\nname'),
                   ('env_key', 'CODEX_HOME'), ('env_key', 'DEEPSEEK_API_KEY=secret-fixture'),
                   ('env_key', 'lower_api_key'), ('wire_api', 'chat'), ('secret', 'secret-fixture'),
                   ('base_url', 'http://api.deepseek.com'), ('base_url', 'https://secret-fixture@api.deepseek.com'),
                   ('base_url', 'https://api.deepseek.com?token=secret-fixture'),
                   ('base_url', 'https://api.deepseek.com#secret-fixture'),
                   ('base_url', 'https://api.deepseek.com?'), ('base_url', 'https://api.deepseek.com#'),
                   ('base_url', 'https://api.deepseek.com:99999'), ('base_url', 'https://api.deepseek.com\\other'),
                   ('base_url', 'https://api.deepseek.com\n'), ('base_url', None)]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError) as caught:
                    Engine.codex_options({'provider': {**PROVIDER, key: value}})
                self.assertNotIn('secret-fixture', str(caught.exception))
        for provider in ({}, [], 'deepseek'):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                Engine.codex_options({'provider': provider})

    def test_catalog_rejects_relative_or_noncanonical_container_paths(self):
        for path in ('catalog.json', 'C:\\catalog.json', '/opt/../workspace/catalog.json',
                     '/opt//catalog.json', '/opt/./catalog.json', '//opt/catalog.json', '/opt/catalog.json\n', None):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'absolute container path'):
                Engine.codex_options({'model_catalog_json': path})


class ProviderExecution(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = {'id': 'fixture', 'triton_version': '3.3', 'container': {'name': 'fixture-only'}}
        self.task = {'schema': 'triton-anchor-local-ci-task-metadata', 'repository': 'anteloper-c/triton-anchor',
                     'task_id': 'provider-fixture', 'task_ref': 'ci/push/main', 'event_kind': 'push', 'pr_number': 0,
                     'target_branch': 'main', 'target_sha': 'a'*40, 'tested_sha': 'a'*40,
                     'head_sha': 'a'*40, 'base_sha': 'a'*40, 'worker_revision_sha': 'b'*40,
                     'captured_at': '2026-09-08T10:00:00Z'}
        self.config = {'local_acceptance': True, 'state_dir': str(self.root / 'state'),
                       'workspace_host': str(self.root / 'workspace'), 'profiles': [self.profile],
                       'codex': copy.deepcopy(SETTINGS)}
        self.relay, self.manager = mock.Mock(), mock.Mock()
        self.relay.current.return_value = True
        self.relay.changed_paths.return_value = ['README.md']
        def checkout(sha, directory):
            directory.mkdir()
            (directory / 'README.md').write_text('frozen fixture\n')
            return {'README.md': digest(directory / 'README.md')}
        self.relay.checkout.side_effect = checkout
        self.engine = Engine(self.config, self.relay, self.manager)
        self.key = 'provider-key-fixture'

    def test_missing_key_fails_before_worker_or_checkout_and_does_not_fallback(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            _, result = self.engine.run(self.task, self.profile)
        self.assertEqual(result['conclusion'], 'error')
        self.assertIn('Codex provider credential is missing: DEEPSEEK_API_KEY', '\n'.join(result['blocking_reasons']))
        self.manager.acquire.assert_not_called()
        self.manager.ensure.assert_not_called()
        self.relay.checkout.assert_not_called()

    def test_initial_and_transient_resume_keep_provider_and_forward_key_only_to_agent(self):
        brokers, calls = [], []
        def broker_factory(*args, **kwargs):
            broker = Broker(*args, **kwargs)
            brokers.append(broker)
            return broker
        def cli(argv, log, **kwargs):
            spec = json.loads(base64.urlsafe_b64decode(argv[-1]))
            calls.append((argv, spec, kwargs['env']))
            if len(calls) == 1:
                records = [{'type': 'thread.started', 'thread_id': SESSION},
                           {'type': 'turn.failed', 'error': {'message': 'Model temporarily overloaded'}}]
            else:
                brokers[0].invoke('finalize', {'review': {'summary': 'Provider fixture completed',
                    'architecture': {'status': 'passed', 'summary': 'Frozen file reviewed',
                                     'evidence': [{'path': 'README.md', 'reason': 'Fixture evidence'}]},
                    'pr_information': {'status': 'not_applicable', 'summary': 'push'}}})
                records = [{'type': 'turn.completed'}]
            log.write_text(''.join(json.dumps(record) + '\n' for record in records))
            return {'returncode': 1 if len(calls) == 1 else 0, 'termination': None,
                    'elapsed_seconds': 0.01, 'log_sha256': digest(log)}
        real_wait = threading.Event.wait
        def fast_backoff(event, timeout=None):
            return event.is_set() if timeout == 30 else real_wait(event, timeout)
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': self.key}), \
             mock.patch('runtime.engine.Broker', side_effect=broker_factory), \
             mock.patch('runtime.engine.execute', side_effect=cli), \
             mock.patch('runtime.control.verify_control', return_value={'tree_sha256': 'fixture', 'verified': False}), \
             mock.patch('runtime.control.verify_container_control', return_value={'verified': False}), \
             mock.patch.object(self.engine, 'docker_run', return_value=''), \
             mock.patch.object(self.engine, 'prepare_agent_home', return_value='/home/agent/.codex'), \
             mock.patch.object(threading.Event, 'wait', new=fast_backoff):
            output, result = self.engine.run(self.task, self.profile)
        self.assertEqual(result['conclusion'], 'success')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]['argv'][:2], ['codex', 'exec'])
        self.assertEqual(calls[1][1]['argv'][:3], ['codex', 'exec', 'resume'])
        for argv, spec, environment in calls:
            for option in Engine.codex_options(SETTINGS):
                self.assertIn(option, spec['argv'])
            self.assertIn('DEEPSEEK_API_KEY', argv[:argv.index('fixture-only')])
            self.assertEqual(environment['DEEPSEEK_API_KEY'], self.key)
            self.assertNotIn('DEEPSEEK_API_KEY', spec['env'])
            self.assertNotIn(self.key, json.dumps(argv))
            self.assertNotIn(self.key, json.dumps(spec))
        for path in output.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.key.encode(), path.read_bytes())
        self.manager.acquire.assert_called_once()
        self.manager.release.assert_called_once()

    def test_publication_resume_uses_same_provider_and_shell_key_exclusion(self):
        agent = self.engine.workspace / 'tasks/current/agent'
        agent.mkdir(parents=True)
        output = self.root / 'output'
        output.mkdir()
        record = output / 'execution.json'
        write_json(record, {'host_task': str(agent.parent), 'profile_id': 'fixture'})
        write_json(output / 'task.json', self.task)
        (output / 'agent-events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': SESSION}) + '\n')
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': self.key}), \
             mock.patch('runtime.engine.execute') as execute, \
             mock.patch('runtime.control.verify_control', return_value={}), \
             mock.patch('runtime.control.verify_container_control'), mock.patch.object(self.engine, 'docker_run'):
            self.engine.recover_publication(record, 'transport fixture')
        argv = execute.call_args.args[0]
        spec = json.loads(base64.urlsafe_b64decode(argv[-1]))
        self.assertEqual(spec['argv'][:3], ['codex', 'exec', 'resume'])
        for option in Engine.codex_options(SETTINGS):
            self.assertIn(option, spec['argv'])
        self.assertIn('DEEPSEEK_API_KEY', argv[:argv.index('fixture-only')])
        self.assertEqual(execute.call_args.kwargs['env']['DEEPSEEK_API_KEY'], self.key)
        self.assertEqual(spec['env'], {})
        self.assertNotIn(self.key, json.dumps(argv))

    def test_basic_tool_docker_exec_never_forwards_provider_environment(self):
        output = self.root / 'broker'
        output.mkdir()
        broker = Broker(self.profile, {'task_id': 'provider-fixture'}, {}, output, lambda: False)
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': self.key}), \
             mock.patch('runtime.broker.execute', return_value={'returncode': 0, 'elapsed_seconds': 0.1}) as execute:
            broker.command('environment', {'argv': ['python', '--version'], 'cwd': '/workspace'})
        argv = execute.call_args.args[0]
        spec = json.loads(base64.urlsafe_b64decode(argv[-1]))
        self.assertNotIn('-e', argv[:argv.index('fixture-only')])
        self.assertNotIn('DEEPSEEK_API_KEY', spec['env'])
        self.assertNotIn(self.key, json.dumps(argv))


if __name__ == '__main__':
    unittest.main()
