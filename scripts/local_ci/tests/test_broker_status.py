"""The agent can inspect progress while a real broker invocation holds its lock."""
import concurrent.futures
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.broker import Broker


class BrokerStatusTests(unittest.TestCase):
    def test_status_does_not_wait_for_a_running_command_or_claim_it_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered, finish, cancelled = threading.Event(), threading.Event(), threading.Event()
            broker = Broker({'container': {'name': 'unused-test-worker'}},
                {'task_id': 'status-test', 'source_dir': str(root), 'artifact_dir': str(root)},
                {'required': [], 'not_applicable': []}, root, cancelled.is_set)

            def execute_command(argv, log, **kwargs):
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                Path(log).write_text('completed test command\n')
                entered.set()
                if not finish.wait(5):
                    raise TimeoutError('test did not release the command')
                return {'returncode': 0, 'termination': None, 'elapsed_seconds': 1,
                        'log_sha256': 'test-digest'}

            with mock.patch('runtime.broker.execute', side_effect=execute_command):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    call = pool.submit(broker.invoke, 'custom_test', {'path': 'probe.py'})
                    try:
                        self.assertTrue(entered.wait(2))
                        status = pool.submit(broker.invoke, 'status', {}).result(timeout=1)
                        self.assertEqual(status['status'], 'running')
                        self.assertEqual(status['active_command']['tool'], 'custom_test')
                        self.assertNotIn('checks', status)
                        self.assertNotIn('receipts', status)
                        cancelled.set()
                        self.assertTrue(pool.submit(broker.invoke, 'status', {}).result(timeout=1)['cancelled'])
                    finally:
                        finish.set()
                    self.assertEqual(call.result(timeout=2)['status'], 'passed')
            status = broker.invoke('status', {})
            self.assertEqual(status['status'], 'ready')
            self.assertEqual(len(status['receipts']), 1)
            self.assertEqual(status['checks']['custom_test']['status'], 'passed')
            self.assertIsNone(broker.active_command)


if __name__ == '__main__':
    unittest.main()
