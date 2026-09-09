"""Malformed reviews remain repairable before a broker closes the task."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control.runtime.broker import Broker  # noqa: E402
from control.runtime.common import digest, read_json  # noqa: E402
from control.runtime.report import build_source_index  # noqa: E402


def review():
    return {'summary': '已完成验证并记录结果。',
            'pr_information': {'status': 'passed', 'summary': '意图和变更一致。'},
            'architecture': {'status': 'passed', 'summary': '架构约束保持一致。',
                             'evidence': [{'path': 'README.md', 'line': 1, 'reason': '架构边界说明'}]},
            'findings': [], 'uncompleted': []}


class FinalizeReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / 'source'
        source.mkdir()
        (source / 'README.md').write_text('Architecture boundary\n', encoding='utf-8')
        source_index = build_source_index(source, {'README.md': digest(source / 'README.md')})
        self.broker = Broker({}, {'event_kind': 'pull_request', 'source_host_dir': str(source), 'source_index': source_index}, {'required': [], 'not_applicable': []},
                             self.root, lambda: False)

    def rejected(self, document, field):
        with self.assertRaisesRegex(ValueError, field):
            self.broker.invoke('finalize', {'review': document})
        self.assertFalse(self.broker.closed)
        self.assertIsNone(self.broker.review)
        self.assertFalse((self.root / 'agent-review.json').exists())

    def test_warning_status_returns_feedback_then_corrected_review_can_submit(self):
        document = review()
        document['pr_information']['status'] = 'warning'
        self.rejected(document, 'pr_information.status')
        self.assertEqual(self.broker.invoke('status', {})['status'], 'ready')
        document['pr_information']['status'] = 'passed'
        self.assertEqual(self.broker.invoke('finalize', {'review': document})['status'], 'submitted')
        self.assertTrue(self.broker.closed)
        self.assertEqual(read_json(self.root / 'agent-review.json'), document)

    def test_required_sections_and_summaries_have_immediate_structure_errors(self):
        cases = [('summary', None), ('summary', ' '), ('architecture', None),
                 ('architecture.status', 'warning'), ('architecture.summary', ''),
                 ('architecture.evidence', []), ('architecture.evidence', {}),
                 ('pr_information', []), ('pr_information.status', ['passed']),
                 ('pr_information.summary', None), ('findings', {}), ('uncompleted', 'pending')]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                document = copy.deepcopy(review())
                keys = path.split('.')
                target = document if len(keys) == 1 else document[keys[0]]
                target[keys[-1]] = value
                self.rejected(document, path)

    def test_failed_checks_and_failed_review_still_accept_failure_report(self):
        self.broker.policy['required'] = ['frontend_build']
        self.broker.checks['frontend_build'] = {'id': 'frontend_build', 'status': 'failed', 'evidence': []}
        document = review()
        document['pr_information']['status'] = 'failed'
        document['architecture']['status'] = 'failed'
        self.assertEqual(self.broker.invoke('finalize', {'review': document})['status'], 'submitted')

    def test_not_applicable_pr_information_is_only_accepted_for_push(self):
        document = review()
        document['pr_information']['status'] = 'not_applicable'
        self.rejected(document, 'pr_information.status')
        self.broker.context['event_kind'] = 'push'
        self.assertEqual(self.broker.invoke('finalize', {'review': document})['status'], 'submitted')

    def test_receipt_ids_return_http_feedback_and_allow_corrected_review(self):
        self.broker.receipts = [{'id': 'command-0001', 'returncode': 0}]
        self.broker.checks = {'environment': {'status': 'passed'}}
        port = self.broker.start(bind='127.0.0.1')
        self.addCleanup(self.broker.stop)
        opener = request.build_opener(request.ProxyHandler({}))
        def submit(document):
            payload = json.dumps({'tool': 'finalize', 'parameters': {'review': document}}).encode()
            return opener.open(request.Request(f'http://127.0.0.1:{port}/', data=payload,
                headers={'Authorization': 'Bearer ' + self.broker.token}), timeout=5)
        document = review()
        document['architecture']['evidence'] = ['command-0001', 'command-0002', 'command-0013']
        with self.assertRaises(error.HTTPError) as caught:
            submit(document)
        self.assertEqual(caught.exception.code, 400)
        feedback = json.loads(caught.exception.read())
        self.assertIn('缺少源码位置证据', feedback['reason'])
        self.assertIn('path', feedback['reason'])
        self.assertIn('reason', feedback['reason'])
        self.assertFalse(self.broker.closed)
        self.assertIsNone(self.broker.review)
        self.assertFalse((self.root / 'agent-review.json').exists())
        with submit(review()) as response:
            self.assertEqual(json.load(response)['status'], 'submitted')
        self.assertEqual(self.broker.receipts, [{'id': 'command-0001', 'returncode': 0}])
        self.assertEqual(self.broker.checks, {'environment': {'status': 'passed'}})

    def test_passed_review_requires_real_relative_source_and_valid_optional_line(self):
        cases = [[], ['command-0001'], [{'path': 1, 'reason': 'reviewed'}],
                 [{'path': 'README.md', 'reason': ' '}], [{'path': 'README.md', 'reason': 7}]]
        cases += [[{'path': path, 'reason': 'reviewed'}] for path in
                  ('missing.md', '../agent-review.json', '/etc/passwd', 'C:/Windows/win.ini', 'dir\\README.md')]
        cases += [[{'path': 'README.md', 'reason': 'reviewed', 'line': line}]
                  for line in (True, 0, 2, '1')]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                document = review()
                document['architecture']['evidence'] = evidence
                self.rejected(document, 'review.architecture.evidence')

    def test_failed_incomplete_review_can_finish_without_invented_evidence(self):
        document = review()
        document['architecture'].update(status='failed', summary='源码尚不可用，未完成架构审查。', evidence=[])
        document['uncompleted'] = ['架构源码证据缺失']
        self.broker.context.pop('source_index')
        self.assertEqual(self.broker.invoke('finalize', {'review': document})['status'], 'submitted')
        self.assertEqual(read_json(self.root / 'agent-review.json'), document)


if __name__ == '__main__':
    unittest.main()
