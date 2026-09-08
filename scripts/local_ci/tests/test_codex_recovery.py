"""Transient CLI failures use real JSONL and one lease; no model/API calls."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.broker import Broker
from runtime.codex_events import read_events
from runtime.common import digest, execute as execute_process, read_json
from runtime.engine import Engine


SESSION = '01a08034-d35e-7651-8a4d-d0a9bc8e6bc1'
CAPACITY = {'type': 'turn.failed', 'error': {'message': 'Selected model is at capacity. Please try a different model.'}}


def events(path, records):
    path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
    return {'returncode': 1 if any(e.get('type') in ('error', 'turn.failed') for e in records) else 0,
            'termination': None, 'elapsed_seconds': 0.1, 'log_sha256': digest(path)}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'state_dir': str(self.root / 'state'), 'workspace_host': str(self.root / 'workspace'),
                       'codex': {'model': 'gpt-5.3-codex-spark', 'reasoning_effort': 'high', 'timeout': 200,
                                 'auto_compact_token_limit': 80000, 'tool_output_token_limit': 4000}}
        self.engine = Engine(self.config, mock.Mock(), mock.Mock())
        self.spec = {'id': 'task-codex', 'argv': ['codex', 'exec', '--json', 'read frozen task context'],
                     'cwd': '/workspace/task/agent', 'env': {'GIT_CONFIG_VALUE_0': '/workspace/task/source'}}
        self.clock = 0
        self.cancelled = threading.Event()
        self.wait = mock.patch.object(self.cancelled, 'wait', side_effect=self.advance)
        self.waiter = self.wait.start()
        self.addCleanup(self.wait.stop)
        self.monotonic = mock.patch('runtime.engine.time.monotonic', side_effect=lambda: self.clock)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)

    def advance(self, seconds):
        self.clock += seconds
        return self.cancelled.is_set()

    def run_attempts(self, sequences, *, termination=None, elapsed=1):
        commands, timeouts = [], []
        def invoke(argv, log, **kwargs):
            commands.append(json.loads(base64.urlsafe_b64decode(argv[-1])))
            timeouts.append(kwargs['timeout'])
            self.clock += elapsed
            result = events(log, sequences[len(commands)-1])
            result['termination'] = termination
            return result
        validate = mock.Mock()
        with mock.patch('runtime.engine.execute', side_effect=invoke):
            result = self.engine.execute_codex(self.spec, ['launcher'], {}, self.root, self.cancelled, validate)
        return result, commands, timeouts, validate

    def test_capacity_resumes_same_session_model_and_budget_with_separate_logs(self):
        result, commands, timeouts, validate = self.run_attempts([
            [{'type': 'thread.started', 'thread_id': SESSION}, CAPACITY], [{'type': 'turn.completed'}]])
        self.assertEqual(result, (0, None))
        resumed = commands[1]
        self.assertEqual(resumed['argv'][:3], ['codex', 'exec', 'resume'])
        self.assertIn(SESSION, resumed['argv'])
        self.assertIn('gpt-5.3-codex-spark', resumed['argv'])
        self.assertIn('model_reasoning_effort="high"', resumed['argv'])
        self.assertIn('model_auto_compact_token_limit=80000', resumed['argv'])
        self.assertIn('tool_output_token_limit=4000', resumed['argv'])
        self.assertEqual(resumed['env'], self.spec['env'])
        self.assertNotEqual(resumed['id'], commands[0]['id'])
        self.assertEqual(timeouts, [200, 169])
        self.waiter.assert_called_once_with(30)
        validate.assert_called_once_with()
        ledger = read_json(self.root / 'agent-recovery.json')
        self.assertIsNone(ledger['error'])
        for attempt in ledger['attempts']:
            self.assertEqual(digest(self.root / attempt['log_path']), attempt['log_sha256'])

    def test_capacity_has_at_most_two_recoveries(self):
        result, commands, timeouts, _ = self.run_attempts([[CAPACITY]] * 3)
        self.assertEqual(len(commands), 3)
        self.assertEqual(timeouts, [200, 169, 108])
        self.assertIn('two service recoveries exhausted', result[1])
        self.assertEqual([c.args[0] for c in self.waiter.call_args_list], [30, 60])
        # No emitted UUID: the fallback explicitly restores frozen context and receipts.
        self.assertEqual(commands[1]['argv'][:2], ['codex', 'exec'])
        self.assertIn('/workspace/task/agent/context.json', commands[1]['argv'][-1])

    def test_cancellation_during_backoff_does_not_launch_again(self):
        def cancel(_):
            self.cancelled.set()
            return True
        self.waiter.side_effect = cancel
        result, commands, _, validate = self.run_attempts([[CAPACITY]])
        self.assertEqual(len(commands), 1)
        self.assertIn('cancelled', result[1])
        validate.assert_not_called()
        self.assertEqual(read_json(self.root / 'agent-recovery.json')['state'], 'finished')

    def test_insufficient_budget_or_execution_timeout_never_retries(self):
        for termination in (None, 'timeout', 'cancelled'):
            with self.subTest(termination=termination):
                self.clock = 0
                self.waiter.reset_mock()
                result, commands, _, _ = self.run_attempts([[CAPACITY]], termination=termination, elapsed=180)
                self.assertEqual(len(commands), 1)
                self.assertIsNotNone(result[1])
                self.waiter.assert_not_called()

    def test_auth_billing_and_ordinary_failures_never_retry(self):
        for error in ({'code': 'credit_balance_exhausted', 'message': 'Rate limit exceeded'},
                      {'code': 'insufficient_quota', 'message': 'Rate limit reached'},
                      {'message': 'Authentication failed: invalid API key'},
                      {'message': 'Permission denied'}, {'message': 'Build failed with exit code 1'}):
            with self.subTest(error=error):
                _, commands, _, _ = self.run_attempts([[{'type': 'turn.failed', 'error': error}]])
                self.assertEqual(len(commands), 1)
        self.waiter.assert_not_called()
        _, commands, _, _ = self.run_attempts([[CAPACITY], [
            {'type': 'turn.failed', 'error': {'message': 'Quota exhausted after capacity recovery'}}]])
        self.assertEqual(len(commands), 2)

    def test_task_or_control_revalidation_prevents_another_launch(self):
        def reject():
            raise ValueError('trusted control changed')
        with mock.patch('runtime.engine.execute', side_effect=lambda argv, log, **kw: events(log, [CAPACITY])) as invoke:
            _, failure = self.engine.execute_codex(self.spec, [], {}, self.root, self.cancelled, reject)
        self.assertEqual(invoke.call_count, 1)
        self.assertIn('trusted control changed', failure)
        self.assertEqual(read_json(self.root / 'agent-recovery.json')['state'], 'finished')

    def test_classifier_ignores_candidate_output_and_clears_recovered_errors(self):
        log = self.root / 'events.jsonl'
        for sequence in (
                [{'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': json.dumps(CAPACITY)}}],
                [CAPACITY, {'type': 'turn.completed'}],
                [CAPACITY, {'type': 'turn.failed', 'error': {'message': 'ordinary failure'}}],
                [{'type': 'error', 'code': 'rate_limit_exceeded'},
                 {'type': 'turn.failed', 'error': {'code': 'invalid_request_error'}}]):
            events(log, sequence)
            _, error = read_events(log)
            self.assertFalse(error and error['retryable'])
        for error in ({'code': 'server_is_overloaded'}, {'code': 'slow_down'},
                      {'message': 'Rate limit reached for requests'}, {'message': 'Model temporarily overloaded'},
                      {'message': 'The model is temporarily overloaded'}):
            events(log, [{'type': 'turn.failed', 'error': error}])
            self.assertTrue(read_events(log)[1]['retryable'])
        events(log, [{'type': 'item.completed', 'thread_id': SESSION},
                     {'type': 'thread.started', 'thread_id': 'arbitrary thread name'}])
        self.assertIsNone(read_events(log)[0])


class ActiveBuildRecoveryTests(unittest.TestCase):
    def test_engine_keeps_one_broker_lease_and_real_build_alive_until_resume(self):
        # Docker/control admission is a test boundary. The build below is a real
        # local subprocess and receipt, not simulated Triton/backend validation.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = {'schema': 'triton-anchor-local-ci-task-metadata', 'repository': 'anteloper-c/triton-anchor',
                    'task_id': 'active-build', 'task_ref': 'ci/push/main', 'event_kind': 'push', 'pr_number': 0,
                    'target_branch': 'main', 'target_sha': 'a'*40, 'tested_sha': 'a'*40,
                    'head_sha': 'a'*40, 'base_sha': 'a'*40, 'worker_revision_sha': 'b'*40,
                    'captured_at': '2026-09-08T10:00:00Z'}
            config = {'local_acceptance': True, 'state_dir': str(root / 'state'),
                      'workspace_host': str(root / 'workspace'), 'codex': {'timeout': 200,
                      'model': 'gpt-5.3-codex-spark', 'reasoning_effort': 'high',
                      'auto_compact_token_limit': 80000, 'tool_output_token_limit': 4000}}
            relay, manager = mock.Mock(), mock.Mock()
            relay.current.return_value, relay.changed_paths.return_value = True, ['README.md']
            def checkout(sha, directory):
                directory.mkdir()
                (directory / 'README.md').write_text('frozen review evidence\n')
                return {'README.md': digest(directory / 'README.md')}
            relay.checkout.side_effect = checkout
            profile = {'id': 'fixture', 'triton_version': '3.3', 'container': {'name': 'fixture'}}
            engine = Engine(config, relay, manager)
            created, calls, build_threads = [], [], []
            build_cancelled = threading.Event()
            entered, release = root / 'entered', root / 'release'
            def broker_factory(*args, **kwargs):
                broker = Broker(*args, **kwargs)
                created.append(broker)
                return broker
            def local_build(argv, log, **kwargs):
                spec = json.loads(base64.urlsafe_b64decode(argv[-1]))
                created[0].active = build_cancelled.set
                return execute_process(spec['argv'], log, timeout=15, cancelled=build_cancelled.is_set)
            def cli(argv, log, **kwargs):
                calls.append(json.loads(base64.urlsafe_b64decode(argv[-1])))
                broker = created[0]
                if len(calls) == 1:
                    code = ('from pathlib import Path; import time; '
                            f'Path({str(entered)!r}).touch(); '
                            f'limit=time.monotonic()+10\nwhile not Path({str(release)!r}).exists() and time.monotonic()<limit: time.sleep(.01)\n'
                            f'assert Path({str(release)!r}).exists()\nprint("build completed")')
                    thread = threading.Thread(target=lambda: broker.command('frontend_build',
                        {'argv': [sys.executable, '-c', code], 'cwd': str(root), 'timeout': 15}))
                    build_threads.append(thread)
                    thread.start()
                    deadline = time.monotonic() + 5
                    while not entered.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(entered.exists())
                    return events(log, [{'type': 'thread.started', 'thread_id': SESSION}, CAPACITY])
                self.assertFalse(broker.closed)
                self.assertFalse(build_cancelled.is_set())
                self.assertTrue(build_threads[0].is_alive())
                manager.release.assert_not_called()
                release.touch()
                build_threads[0].join(5)
                self.assertFalse(build_threads[0].is_alive())
                self.assertEqual(broker.receipts[0]['returncode'], 0)
                broker.invoke('finalize', {'review': {'summary': 'Real process survived service recovery',
                    'architecture': {'status': 'passed', 'summary': 'Frozen file checked',
                                     'evidence': [{'path': 'README.md', 'reason': 'fixture evidence'}]},
                    'pr_information': {'status': 'not_applicable', 'summary': 'push'}}})
                return events(log, [{'type': 'turn.completed'}])
            real_wait = threading.Event.wait
            def fast_backoff(event, timeout=None):
                return event.is_set() if timeout == 30 else real_wait(event, timeout)
            try:
                with mock.patch('runtime.engine.Broker', side_effect=broker_factory), \
                     mock.patch('runtime.engine.execute', side_effect=cli), \
                     mock.patch('runtime.broker.execute', side_effect=local_build), \
                     mock.patch('runtime.control.verify_control', return_value={'tree_sha256': 'fixture', 'verified': False}), \
                     mock.patch('runtime.control.verify_container_control', return_value={'verified': False}), \
                     mock.patch.object(engine, 'docker_run', return_value=''), \
                     mock.patch.object(engine, 'prepare_agent_home', return_value='/home/agent/.codex'), \
                     mock.patch.object(threading.Event, 'wait', new=fast_backoff):
                    output, result = engine.run(task, profile)
            finally:
                release.touch()
                for thread in build_threads:
                    thread.join(5)
            self.assertEqual(len(created), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]['argv'][:2], ['codex', 'exec'])
            self.assertEqual(calls[1]['argv'][:3], ['codex', 'exec', 'resume'])
            for spec in calls:
                for option in ('gpt-5.3-codex-spark', 'model_reasoning_effort="high"',
                               'model_auto_compact_token_limit=80000', 'tool_output_token_limit=4000'):
                    self.assertIn(option, spec['argv'])
            manager.acquire.assert_called_once()
            manager.release.assert_called_once()
            self.assertEqual(result['conclusion'], 'success')
            self.assertTrue(result['source_unchanged'])
            self.assertEqual(len(read_json(output / 'evidence.json')), 1)


if __name__ == '__main__':
    unittest.main()
