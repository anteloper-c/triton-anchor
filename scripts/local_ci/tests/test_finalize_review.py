"""Malformed reviews remain repairable before a broker closes the task."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.broker import Broker  # noqa: E402
from runtime.common import read_json  # noqa: E402


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
        self.broker = Broker({}, {'event_kind': 'pull_request'}, {'required': [], 'not_applicable': []},
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


if __name__ == '__main__':
    unittest.main()
