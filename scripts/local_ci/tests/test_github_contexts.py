"""Regression: branch CI on a PR head must never replace its required status."""
from __future__ import annotations

import copy
import json
import sys
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.local_ci.tests.test_github_result import receiver, result_fixture

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / '.github/workflows'
sys.path.insert(0, str(ROOT / 'scripts/local_ci/control/integration'))
from configure_required_checks import managed_rulesets, ruleset_payload

NODE = shutil.which('node')
BASH = shutil.which('bash') if os.name != 'nt' else next(
    (path for path in (r'C:\msys64\usr\bin\bash.exe',) if Path(path).is_file()), None)


class StatusAPI:
    def __init__(self, expected):
        self.expected = expected
        self.statuses = {}
        self.writes = []

    def call(self, method, path, data=None):
        if method != 'GET':
            self.writes.append((method, path, data))
            if '/statuses/' in path:
                self.statuses[path.rsplit('/', 1)[1], data['context']] = data['state']
            return {}
        task = self.expected
        if '/branches/' in path:
            return {'commit': {'sha': task['head_sha']}}
        if '/pulls/' in path:
            return {'state': 'open', 'draft': False, 'head': {'sha': task['head_sha']},
                    'base': {'sha': task['base_sha'], 'ref': task['target_branch']}}
        if '/git/ref/' in path:
            return {'object': {'sha': task['tested_sha']}}
        if '/git/commits/' in path:
            return {'parents': [{'sha': task['base_sha']}, {'sha': task['head_sha']}]}
        if '/comments?' in path:
            return []
        raise AssertionError((method, path))


class ResultContextTests(unittest.TestCase):
    def test_branch_failure_cannot_replace_successful_pr_on_the_same_head(self):
        _, expected, result = result_fixture()
        api = StatusAPI(expected)
        receiver.publish(api, expected, result, 'success', 'https://example.invalid/result')
        for kind, state in (('push', 'error'), ('full', 'failure')):
            branch = dict(expected, event_kind='push', pr_number=0, task_ref=f'ci/{kind}/topic',
                          tested_sha=expected['head_sha'], base_sha=expected['head_sha'])
            receiver.publish(api, branch, dict(result, conclusion=state), state, 'https://example.invalid/result')
            self.assertEqual(api.statuses[expected['head_sha'], f'local-ci/summary/{kind}'], state)
            self.assertEqual(api.statuses[expected['head_sha'], 'local-ci/summary'], 'success')
        self.assertEqual(api.statuses[expected['tested_sha'], 'local-ci/summary'], 'success')
        self.assertEqual(sum('/issues/' in path for _, path, _ in api.writes), 1)

    def test_context_override_or_inconsistent_identity_fails_before_any_write(self):
        _, expected, result = result_fixture()
        for kind in ('push', 'full'):
            task = dict(expected, event_kind='push', pr_number=0, task_ref=f'ci/{kind}/topic')
            for reserved in ('local-ci/summary', 'local-ci/basic', 'local-ci/api', 'local-ci/security'):
                with self.subTest(kind=kind, reserved=reserved):
                    api = StatusAPI(expected)
                    with self.assertRaisesRegex(ValueError, 'context'):
                        receiver.publish(api, task, result, 'error', '', reserved)
                    self.assertFalse(api.writes)
        for updates in ({'task_ref': 'ci/push/topic'}, {'event_kind': 'push'}, {'pr_number': 20}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                receiver.summary_context(dict(expected, **updates))


@unittest.skipUnless(NODE, 'Node.js is required to execute GitHub script regressions')
class WorkflowContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = yaml.safe_load((WORKFLOWS / 'ci-gateway.yml').read_text(encoding='utf-8'))
        cls.dispatch = yaml.safe_load((WORKFLOWS / 'dispatch-local-ci.yml').read_text(encoding='utf-8'))
        cls.receive = yaml.safe_load((WORKFLOWS / 'receive-local-ci-result.yml').read_text(encoding='utf-8'))

    def evaluate(self, expression, inputs):
        if not expression.startswith('${{'):
            return expression
        # These context selectors deliberately use only string comparisons and && / ||.
        script = ('const inputs = new Proxy(' + json.dumps(inputs) + ', {get:(o,k)=>o[k]??""});'
                  'const vars={LOCAL_CI_CONTEXT:"local-ci/security"};'
                  'const startsWith=(value,prefix)=>String(value).startsWith(prefix);'
                  'process.stdout.write(String(' + expression[3:-2].strip() + '));')
        return subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True).stdout

    def context_for(self, workflow, job, inputs):
        block = workflow['jobs'][job]
        env = block.get('env') or block['steps'][0]['env']
        return self.evaluate(env.get('CONTEXT') or env['STATUS_CONTEXT'], inputs)

    def test_automatic_pushes_are_limited_to_maintained_branches(self):
        triggers = self.dispatch.get('on', self.dispatch.get(True))
        self.assertEqual(triggers['push']['branches'], ['main', 'ci_repo'])
        self.assertIn('workflow_dispatch', triggers)

    def run_js(self, job, inputs, extra=None):
        step = self.gateway['jobs'][job]['steps'][0]
        env = dict(os.environ, REQUESTED_BRANCH='topic', REQUESTED_SHA='', FLAGGEMS_MODE='sample',
                   GATEWAY_KIND='triton-anchor-ci-gateway', REQUESTED_KIND='invalid',
                   FALLBACK_WORKER_BRANCH='ci_repo', FALLBACK_PUSH_ENABLED='true',
                   TESTED_SHA='b' * 40, SHA='b' * 40, EXPECTED_HEAD_SHA='b' * 40,
                   PR_NUMBER='', TASK_REF=inputs.get('task_ref', 'ci/push/topic'),
                   STATUS_CONTEXT=self.context_for(self.gateway, job, inputs))
        env.update(extra or {})
        harness = '''
const writes=[];
const context={repo:{owner:'anteloper-c',repo:'triton-anchor'},ref:'refs/heads/main',actor:'maintainer',serverUrl:'https://example.invalid'};
const core={setOutput(){},notice(){},warning(){},info(){}};
const github={rest:{repos:{
  createCommitStatus:async(value)=>writes.push(value),
  get:async()=>({data:{default_branch:'main'}}),
  getCollaboratorPermissionLevel:async()=>({data:{permission:'admin'}}),
  getBranch:async()=>({data:{commit:{sha:'b'.repeat(40)}}}),
  getContent:async()=>{throw new Error('fixture: unavailable worker');}
}}};
(async()=>{try {
''' + step['with']['script'] + '\n} catch(error) {} process.stdout.write(JSON.stringify(writes));})();'
        return json.loads(subprocess.run([NODE, '-e', harness], env=env, check=True, capture_output=True, text=True).stdout)

    def test_dispatch_and_receiver_choose_context_from_event_not_caller_override(self):
        for kind in ('push', 'full', 'pr'):
            inputs = dict(pr_number='19' if kind == 'pr' else '', flaggems_mode='full' if kind == 'full' else 'sample',
                          task_ref='ci/pr-19/topic' if kind == 'pr' else f'ci/{kind}/topic', context='local-ci/security')
            expected = 'local-ci/summary' + (f'/{kind}' if kind != 'pr' else '')
            for workflow, job in ((self.dispatch, 'dispatch-local-ci'), (self.receive, 'receive'), (self.gateway, 'validate-receive')):
                with self.subTest(kind=kind, job=job):
                    self.assertEqual(self.context_for(workflow, job, inputs), expected)
            if kind != 'pr':
                for job in ('route-manual-push', 'validate-push', 'push-failure-status'):
                    with self.subTest(kind=kind, job=job):
                        self.assertEqual(self.context_for(self.gateway, job, inputs), expected)

    def test_actual_gateway_failure_scripts_never_write_pr_context_for_branch_tasks(self):
        for kind in ('push', 'full'):
            inputs = {'flaggems_mode': 'full' if kind == 'full' else 'sample', 'task_ref': f'ci/{kind}/topic'}
            for job in ('route-manual-push', 'validate-push', 'push-failure-status', 'validate-receive'):
                with self.subTest(kind=kind, job=job):
                    writes = self.run_js(job, inputs, {'FLAGGEMS_MODE': inputs['flaggems_mode'], 'PR_NUMBER': '19'})
                    self.assertEqual(len(writes), 1)
                    self.assertEqual((writes[0]['sha'], writes[0]['context'], writes[0]['state']),
                                     ('b' * 40, f'local-ci/summary/{kind}', 'error'))

    @unittest.skipUnless(BASH, 'Bash is required to exercise receiver continuation')
    def test_actual_receiver_retry_and_timeout_preserve_branch_context(self):
        step = next(step for step in self.receive['jobs']['receive']['steps'] if step.get('id') == 'bridge')
        for kind in ('push', 'full'):
            for attempt in ('1', '6'):
                with self.subTest(kind=kind, attempt=attempt), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory)
                    # Mock transport only; execute the shipped continuation / terminal shell body.
                    script = 'python() { return 3; }\ngh() { printf "%s\\n" "$*" >> "$CALLS"; }\n' + step['run']
                    source = path / 'step.sh'
                    source.write_text(script, encoding='utf-8', newline='\n')
                    env = dict(os.environ, CONTEXT=self.context_for(self.receive, 'receive', {'task_ref': f'ci/{kind}/topic'}),
                               TASK_REF=f'ci/{kind}/topic', PR_NUMBER='', EXPECTED_HEAD_SHA='b' * 40,
                               TESTED_SHA='b' * 40, ATTEMPT=attempt, MAX_ATTEMPTS='6', SOURCE_BRANCH='topic',
                               REPO='anteloper-c/triton-anchor', GITHUB_REF_NAME='ci_repo', CALLS=str(path / 'calls'),
                               GITHUB_OUTPUT=str(path / 'output'))
                    process = subprocess.run([BASH, str(source)], env=env, capture_output=True, text=True)
                    self.assertEqual(process.returncode, 0 if attempt == '1' else 1, process.stderr)
                    calls = (path / 'calls').read_text()
                    self.assertIn(f'context=local-ci/summary/{kind}', calls)
                    self.assertNotIn('context=local-ci/summary ', calls)
                    self.assertEqual(calls.count('statuses/' + 'b' * 40), 1)
                    self.assertEqual('mode=receive' in calls, attempt == '1')


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


if __name__ == '__main__':
    unittest.main()
