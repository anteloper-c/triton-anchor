"""Renaming the gate must not duplicate or absorb unrelated repository rules."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
from configure_required_checks import MANAGED_NAME, managed_rulesets, ruleset_payload


class SettingsTests(unittest.TestCase):
    def test_equivalent_gate_is_selected_without_matching_its_old_name(self):
        desired = ruleset_payload(['main', 'ci_repo'], 15368)
        old = {'id': 42, 'source': 'anteloper-c/triton-anchor', 'target': 'branch', 'name': 'Previous display name'}
        calls = []
        class API:
            def call(self, method, path):
                calls.append((method, path))
                document = copy.deepcopy(desired)
                document['conditions']['ref_name']['include'].reverse()
                document['rules'][0]['parameters']['required_status_checks'].reverse()
                return document
        self.assertEqual(managed_rulesets(API(), 'repos/anteloper-c/triton-anchor', old['source'], [old], desired), [old])
        self.assertEqual(calls, [('GET', 'repos/anteloper-c/triton-anchor/rulesets/42')])

    def test_other_target_or_bypass_rules_are_never_selected_for_rename(self):
        desired = ruleset_payload(['main', 'ci_repo'], 15368)
        old = {'id': 42, 'source': 'anteloper-c/triton-anchor', 'target': 'branch', 'name': 'Other gate'}
        for field, value in [('bypass_actors', [{'actor_id': 1}]), ('conditions', {'ref_name': {'include': ['refs/heads/main']}})]:
            document = {**copy.deepcopy(desired), field: value}
            class API:
                def call(self, method, path):
                    return document
            with self.subTest(field=field):
                self.assertEqual(managed_rulesets(API(), 'repos/anteloper-c/triton-anchor', old['source'], [old], desired), [])

    def test_managed_display_name_is_stable(self):
        self.assertEqual(MANAGED_NAME, 'Local CI mandatory checks')


if __name__ == '__main__':
    unittest.main()
