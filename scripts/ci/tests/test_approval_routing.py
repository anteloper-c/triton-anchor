"""Execute the workflow's approval conditions and card script without API writes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest

from test_gateway_contract import job as workflow_job

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node.js is required to exercise actual GitHub scripts')
class ApprovalRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = (ROOT / '.github/workflows/ci-gateway.yml').read_text(encoding='utf-8')

    def condition(self, job, needs, *, cancelled=False):
        match = re.search(r'(?m)^    if: \$\{\{(.+)\}\}\s*$', workflow_job(self.gateway, job))
        self.assertIsNotNone(match, f'Job {job} must expose its actual approval condition')
        expression = match[1].strip()
        expression = re.sub(r'needs\.([a-zA-Z0-9_-]+)', lambda m: 'needs[' + json.dumps(m[1]) + ']', expression)
        script = ('const needs=' + json.dumps(needs) + '; const always=()=>true; const cancelled=()=>'+json.dumps(cancelled)+';'
                  'const github={event_name:"workflow_dispatch"};'
                  'const inputs={mode:"dispatch",expected_head_sha:"a",worker_revision_sha:"b"};'
                  'process.stdout.write(JSON.stringify(Boolean(' + expression + ')));')
        return json.loads(subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True).stdout)

    def needs(self, external=False):
        needs = {key: {'result': 'success'} for key in ('validate-dispatch', 'basic-and-api', 'security-gate', 'security-result')}
        needs['validate-dispatch']['outputs'] = {'requires_approval': 'true' if external else 'false'}
        needs.update({'approval-review-card': {'result': 'skipped'}, 'approve-external-fork': {'result': 'skipped'},
                      'dispatch': {'result': 'skipped'}})
        return needs

    def test_internal_pr_skips_both_approval_jobs_and_dispatches(self):
        needs = self.needs()
        self.assertFalse(self.condition('approval-review-card', needs))
        self.assertFalse(self.condition('approve-external-fork', needs))
        self.assertTrue(self.condition('dispatch', needs))
        self.assertFalse(self.condition('dispatch-failure-status', needs))
        needs['validate-dispatch']['outputs']['requires_approval'] = ''
        self.assertFalse(self.condition('dispatch', needs))

    def test_external_pr_cannot_dispatch_before_card_and_environment_approval(self):
        needs = self.needs(True)
        self.assertTrue(self.condition('approval-review-card', needs))
        self.assertTrue(self.condition('approve-external-fork', needs))
        self.assertFalse(self.condition('dispatch', needs))
        needs['approval-review-card']['result'] = 'success'
        self.assertFalse(self.condition('dispatch', needs))
        needs['approve-external-fork']['result'] = 'success'
        self.assertTrue(self.condition('dispatch', needs))

    def test_skipping_internal_approval_does_not_bypass_preflight_or_status_failures(self):
        for external in (False, True):
            ready = self.needs(external)
            if external:
                ready['approval-review-card']['result'] = ready['approve-external-fork']['result'] = 'success'
            for job in ('validate-dispatch', 'basic-and-api', 'security-gate', 'security-result'):
                for status in ('failure', 'cancelled', 'skipped'):
                    with self.subTest(external=external, job=job, status=status):
                        needs = copy.deepcopy(ready)
                        needs[job]['result'] = status
                        self.assertFalse(self.condition('dispatch', needs))
            ready['security-result']['result'] = 'failure'
            self.assertTrue(self.condition('dispatch-failure-status', ready))
            self.assertFalse(self.condition('dispatch-failure-status', ready, cancelled=True))
        for job in ('approval-review-card', 'approve-external-fork'):
            for status in ('failure', 'cancelled'):
                with self.subTest(job=job, status=status):
                    needs = self.needs(True)
                    needs[job]['result'] = status
                    self.assertFalse(self.condition('dispatch', needs))
                    self.assertTrue(self.condition('dispatch-failure-status', needs))

    def card(self, repository='contributor/triton-anchor', *, head='a' * 40, base='b' * 40, reviewers=True):
        block = workflow_job(self.gateway, 'approval-review-card')
        match = re.search(r'(?m)^          script: \|\n((?:(?: {12}.*)?\n)*)', block)
        self.assertIsNotNone(match, 'Approval card must expose its actual GitHub script')
        script = textwrap.dedent(match[1])
        fixture = {'pull': {'number': 19, 'state': 'open', 'draft': False,
                            'head': {'sha': head, 'repo': {'full_name': repository}}, 'base': {'sha': base}},
                   'environment': {'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'id': 1}]}] if reviewers else []}}
        harness = ('const fixture=' + json.dumps(fixture) + '; const writes=[]; const reads=[];'
                   'const context={repo:{owner:"anteloper-c",repo:"triton-anchor"},serverUrl:"https://github.com",runId:101};'
                   'const github={rest:{pulls:{get:async()=>({data:fixture.pull})},repos:{getEnvironment:async(args)=>{reads.push(args);return {data:fixture.environment};}},'
                   'issues:{listComments(){},updateComment:async(value)=>writes.push(value),createComment:async(value)=>writes.push(value)}},paginate:async()=>[]};'
                   '(async()=>{let error=null;try{' + script + '}catch(e){error=e.message;}'
                   'process.stdout.write(JSON.stringify({writes,reads,error}));})();')
        env = dict(os.environ, PR_NUMBER='19', HEAD_SHA='a' * 40, BASE_SHA='b' * 40,
                   TESTED_SHA='c' * 40, CONTROL_SHA='d' * 40, APPROVAL_ENVIRONMENT='local-ci-fork-approval')
        return json.loads(subprocess.run([NODE, '-e', harness], env=env, check=True, capture_output=True, text=True).stdout)

    def test_card_requires_external_identity_frozen_revisions_and_reviewers(self):
        result = self.card()
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['writes']), 1)
        self.assertEqual(result['reads'][0]['environment_name'], 'local-ci-fork-approval')
        body = result['writes'][0]['body']
        for value in ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40, '检查证据和审批入口'):
            self.assertIn(value, body)
        for mutation in ({'repository': 'anteloper-c/triton-anchor'}, {'repository': None},
                         {'head': 'e' * 40}, {'base': 'e' * 40}, {'reviewers': False}):
            with self.subTest(mutation=mutation):
                result = self.card(**mutation)
                self.assertIsNotNone(result['error'])
                self.assertEqual(result['writes'], [])


if __name__ == '__main__':
    unittest.main()
