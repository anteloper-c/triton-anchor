from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / '.github/workflows'


def job(text: str, name: str) -> str:
    match = re.search(r'^  ' + re.escape(name) + r':\n(.*?)(?=^  [a-zA-Z][a-zA-Z0-9_-]*:|\Z)', text, re.M | re.S)
    if not match:
        raise AssertionError(f'Job {name} does not exist')
    return match.group(1)


def dependencies(block: str) -> set[str]:
    match = re.search(r'^    needs:([^\n]*)\n((?:      - [^\n]*\n)*)', block, re.M)
    if not match:
        return set()
    inline = match.group(1).strip().strip('[]')
    return {item.strip() for item in inline.split(',') if item.strip()} if inline else set(re.findall(r'      - ([^\n]+)', match.group(2)))


class GatewayContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = (WORKFLOWS / 'ci-gateway.yml').read_text(encoding='utf-8')
        cls.dispatch = (WORKFLOWS / 'dispatch-local-ci.yml').read_text(encoding='utf-8')
        cls.receive = (WORKFLOWS / 'receive-local-ci-result.yml').read_text(encoding='utf-8')
        cls.prechecks = (WORKFLOWS / 'local-ci-prechecks.yml').read_text(encoding='utf-8')

    def test_interface_carries_immutable_identity(self):
        declarations = re.findall(r"^  WORKER_CONTRACT: '([^'\r\n]+)'$", self.gateway, re.M)
        self.assertEqual(len(declarations), 1)
        manifest = json.loads(declarations[0])
        self.assertEqual(manifest['kind'], 'triton-anchor-ci-gateway')
        self.assertIn('task-result', manifest['capabilities'])
        self.assertIn('GATEWAY_KIND: "triton-anchor-ci-gateway"', self.gateway)
        self.assertNotIn('gateway_contract_version', self.gateway)
        self.assertNotIn('schema_version', manifest)
        for name in ('task_id', 'expected_head_sha', 'comparison_base_sha', 'tested_sha', 'worker_revision_sha'):
            self.assertRegex(self.gateway, r'(?m)^      ' + name + ':$')

    def test_all_preflight_gates_are_in_the_admission_dependency_chain(self):
        self.assertEqual(dependencies(job(self.gateway, 'basic-and-api')), {'validate-dispatch'})
        self.assertEqual(dependencies(job(self.prechecks, 'api')), {'basic'})
        self.assertEqual(dependencies(job(self.gateway, 'security-gate')), {'validate-dispatch', 'basic-and-api'})
        self.assertTrue({'security-gate', 'basic-and-api', 'validate-dispatch'} <= dependencies(job(self.gateway, 'approval-review-card')))
        self.assertTrue({'security-gate', 'approval-review-card', 'approve-external-fork'} <= dependencies(job(self.gateway, 'dispatch')))

    def test_pr_information_precedes_build_checks(self):
        validate = job(self.gateway, 'validate-dispatch')
        self.assertIn("meaningful(pull.title).length < 8", validate)
        self.assertIn("meaningful(pull.body).length < 30", validate)
        self.assertIn("pull.head.sha.toLowerCase()", validate)
        self.assertIn('parents[0]', validate)
        self.assertIn('parents[1]', validate)

    def test_basic_and_api_execute_real_checks_on_frozen_source(self):
        basic, api = job(self.prechecks, 'basic'), job(self.prechecks, 'api')
        self.assertIn('ruff check', basic)
        self.assertIn('python -m pytest python/triton_anchor/tests/', basic)
        self.assertIn('ref: ${{ inputs.tested_sha }}', basic)
        self.assertIn('persist-credentials: false', basic)
        self.assertNotIn('statuses: write', basic)
        self.assertIn('control/scripts/api_contract/check_public_api.py', api)
        self.assertIn('--base-root base --candidate-root candidate', api)
        self.assertIn('ref: ${{ inputs.trusted_ref }}', api)
        self.assertIn('candidate_scope=()', api)
        self.assertIn('if [ -f base/api_contract/public_api.json ]; then', api)
        self.assertIn('elif [ -f candidate/api_contract/public_api.json ]; then', api)

    def test_security_status_has_a_terminal_failure_path(self):
        status = job(self.gateway, 'security-result')
        self.assertIn('if: ${{ always()', status)
        self.assertIn("context: 'local-ci/security'", status)
        self.assertIn("result === 'failure' ? 'failure' : 'error'", status)
        self.assertIn('security-result', dependencies(job(self.gateway, 'approval-review-card')))

    def test_external_approval_is_after_security_and_cannot_be_claimed_by_text(self):
        approval = job(self.gateway, 'approve-external-fork')
        self.assertIn('security-gate', dependencies(approval))
        self.assertIn('local-ci-fork-approval', approval)
        self.assertIn("needs.validate-dispatch.outputs.requires_approval == 'true'", approval)
        self.assertIn("pull.head.repo.full_name !== `${owner}/${repo}`", job(self.gateway, 'validate-dispatch'))
        self.assertIn("rule.type === 'required_reviewers'", job(self.gateway, 'approval-review-card'))
        self.assertIn("if: ${{ needs.validate-dispatch.outputs.requires_approval == 'true' }}", job(self.gateway, 'approval-review-card'))
        self.assertIn('security-result', dependencies(job(self.gateway, 'dispatch')))
        self.assertIn("needs.approve-external-fork.result == 'success'", job(self.gateway, 'dispatch'))
        self.assertNotIn('approve-external-fork', dependencies(job(self.gateway, 'route-pull-request')))

    def test_metadata_and_code_are_published_atomically_by_trusted_builder(self):
        self.assertIn('scripts/local_ci/runtime/build_task_metadata.py', self.dispatch)
        self.assertIn('${WORKER_REVISION_SHA}:scripts/local_ci/runtime/build_task_metadata.py', self.dispatch)
        self.assertIn('git push --atomic --force gitee-ci', self.dispatch)
        self.assertNotIn('execution_mode=codex_only', self.dispatch)
        self.assertIn('PREFLIGHT_PASSED: ${{ inputs.preflight_passed }}', self.dispatch)
        self.assertIn('EXTERNAL_APPROVAL: ${{ inputs.external_approval }}', self.dispatch)

    def test_manual_full_remains_a_real_branch_task(self):
        self.assertIn('options: [sample, full]', self.dispatch)
        self.assertIn('task_ref="ci/full/${HEAD_REF}"', self.dispatch)
        self.assertIn('metadata_ref="ci/meta/full/${HEAD_REF}"', self.dispatch)
        self.assertIn('fetch_ref="refs/heads/${HEAD_REF}"', self.dispatch)
        self.assertIn('flaggems_mode: flaggemsMode', job(self.gateway, 'route-manual-push'))
        self.assertIn("flaggems_mode: ${{ inputs.flaggems_mode || 'sample' }}", job(self.gateway, 'dispatch-push'))

    def test_receiver_uses_task_path_and_forwards_task_identity(self):
        self.assertIn('scripts/local_ci/runtime/receive_result.py', self.receive)
        self.assertNotIn('scripts/local_ci/results/', self.receive)
        self.assertIn('--task-id "${TASK_ID}"', self.receive)
        self.assertIn('--worker-revision-sha "${WORKER_REVISION_SHA}"', self.receive)
        self.assertIn('-f task_id="${TASK_ID}"', self.receive)
        self.assertNotIn('非阻塞', self.receive)
        self.assertIn('[ "${OVERALL_STATUS}" = success ]', self.receive)

    def test_worker_routes_and_control_sha_stay_bound(self):
        self.assertIn("let worker = await inspectWorker(pull.base.ref, true)", self.gateway)
        self.assertIn("FALLBACK_WORKER_BRANCH: ${{ vars.LOCAL_CI_FALLBACK_WORKER_BRANCH || 'ci_repo' }}", self.gateway)
        self.assertIn('process.env.WORKER_REVISION_SHA.toLowerCase()', self.gateway)
        self.assertIn("manifest.role === 'router'", self.gateway)
        self.assertIn("!Object.hasOwn(manifest, 'kind')", self.gateway)
        self.assertIn('use the verified fallback', self.gateway)

    def test_watchdog_implementation_is_reusable_on_control_branch(self):
        source = (WORKFLOWS / 'local-ci-watchdog.yml').read_text(encoding='utf-8')
        self.assertIn('  workflow_call:', source)
        self.assertNotIn('  schedule:', source)
        self.assertIn('ref: ci_repo', source)
        self.assertIn('python -m scripts.local_ci.maintenance.watchdog', source)

    def test_lifecycle_cancellation_reaches_local_worker(self):
        cancellation = job(self.gateway, 'cancel')
        self.assertIn('cancellation.json', cancellation)
        self.assertIn('ci/cancel/${TASK_REF#ci/}', cancellation)
        self.assertIn('cancelled_at', cancellation)
        self.assertIn('target_branch', cancellation)
        self.assertIn("action === 'synchronize' && shaPattern.test(previousHeadSha) ? previousHeadSha", self.gateway)
        self.assertIn('Date.parse(run.created_at) > Date.parse(cancellationRun.created_at)', cancellation)

    def test_new_workflows_never_default_to_existing_gitee_repositories(self):
        for name in ('ci-gateway.yml', 'dispatch-local-ci.yml', 'receive-local-ci-result.yml', 'backend-status-pages.yml'):
            source = (WORKFLOWS / name).read_text(encoding='utf-8')
            self.assertNotIn('likehupochuan', source, name)
            self.assertNotIn("|| 'triton-anchor-local-ci-results'", source, name)
            self.assertIn('heron-mc', source, name)

    def test_no_ephemeral_delivery_or_local_ci_harness_is_left_active(self):
        delivery = (WORKFLOWS / 'delivery-ci.yml').read_text(encoding='utf-8')
        contracts = (WORKFLOWS / 'local_ci.yml').read_text(encoding='utf-8')
        self.assertNotIn('docker run', delivery)
        self.assertNotIn('RACE-org/', delivery)
        self.assertNotIn('codex_ai/', contracts)
        self.assertIn('scripts/local_ci/tests scripts/ci/tests', contracts)


if __name__ == '__main__':
    unittest.main()
