"""Router checks inspect candidate syntax; worker checks retain their real suites."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import control_plane
from runtime.broker import Broker
from runtime.policy import BACKEND_TOOLS, minimum_checks


def router(root):
    directory = root / '.github/workflows'
    directory.mkdir(parents=True)
    inputs = '\n'.join(f'      {name}: {{type: string}}' for name in (
        'task_id', 'expected_head_sha', 'comparison_base_sha', 'tested_sha', 'worker_revision_sha'))
    jobs = '\n'.join(f'  {name}:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo syntax-only' for name in (
        'route-cancellation', 'prepare-route', 'route-pull-request', 'route-manual-push', 'route-failure-status'))
    (directory / 'ci-gateway.yml').write_text(
        "on:\n  schedule:\n    - cron: '17,47 * * * *'\n  pull_request_target:\n  workflow_dispatch:\n    inputs:\n" + inputs +
        '\nenv:\n  GATEWAY_KIND: triton-anchor-ci-gateway\njobs:\n' + jobs +
        '\n  watchdog:\n    uses: anteloper-c/triton-anchor/.github/workflows/local-ci-watchdog.yml@ci_repo\n', encoding='utf-8')
    for name in ('api-breaking-notify.yml', 'ci.yml', 'upstream_watch.yml'):
        (directory / name).write_text('on: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo checked\n', encoding='utf-8')


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        router(self.root)
        self.context = {'source_host_dir': str(self.root), 'source_dir': '/workspace/candidate',
                        'target_branch': 'main', 'python_bin': '/trusted/task_python'}

    def test_router_exception_requires_main_without_worker_scripts(self):
        command = control_plane.plan(self.context)['commands'][0]
        self.assertEqual(command['argv'][:2], ['/opt/ci-venv/bin/python', '-I'])
        self.assertEqual(command['argv'][-1], '/workspace/candidate')
        for branch in ('ci_repo', 'feature', ''):
            with self.subTest(branch=branch):
                self.assertIn('scripts/local_ci/tests', control_plane.plan({**self.context, 'target_branch': branch})['commands'][0]['argv'])

    def test_partial_worker_tree_never_falls_back_to_router(self):
        (self.root / 'scripts/local_ci/tests').mkdir(parents=True)
        self.assertIn('scripts/ci/tests', control_plane.plan(self.context)['commands'][0]['argv'])
        with self.assertRaisesRegex(ValueError, 'cannot replace worker tests'):
            control_plane.check_router(self.root)

    def test_yaml_duplicates_and_missing_jobs_fail(self):
        for text in ('on: push\non: pull_request\njobs: {}', 'on: push\njobs: {}', 'on: [broken'):
            with self.subTest(text=text), self.assertRaises(Exception):
                control_plane.check_workflow('invalid.yml', control_plane.load_workflow(text))

    def test_unknown_and_cyclic_job_dependencies_fail(self):
        for dependencies in ({'a': 'missing'}, {'a': 'b', 'b': 'a'}):
            jobs = {name: {'runs-on': 'ubuntu-latest', 'needs': dependency, 'steps': [{'run': 'echo parsed'}]}
                    for name, dependency in dependencies.items()}
            with patch.object(control_plane, 'syntax'), self.assertRaisesRegex(ValueError, 'dependenc'):
                control_plane.check_workflow('jobs.yml', {'on': 'push', 'jobs': jobs})

    def test_router_requires_frozen_identity_and_trusted_watchdog(self):
        gateway = self.root / '.github/workflows/ci-gateway.yml'
        original = gateway.read_text(encoding='utf-8')
        gateway.write_text(original.replace('tested_sha:', 'unbound_sha:'), encoding='utf-8')
        with patch.object(control_plane, 'syntax'), self.assertRaisesRegex(ValueError, 'immutable identity'):
            control_plane.check_router(self.root)
        gateway.write_text(original, encoding='utf-8')
        watchdog = gateway
        watchdog.write_text(watchdog.read_text(encoding='utf-8').replace('@ci_repo', '@main'), encoding='utf-8')
        with patch.object(control_plane, 'syntax'), self.assertRaisesRegex(ValueError, 'delegate'):
            control_plane.check_router(self.root)

    def test_broker_preserves_failure_receipt(self):
        broker = Broker({}, self.context, {'required': ['control_plane'], 'not_applicable': []},
                        self.root / 'output', lambda: False)
        with patch.object(broker, 'command', return_value={'id': 'syntax-failure', 'returncode': 1, 'termination': None}):
            result = broker.invoke('control_plane', {})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence'], ['syntax-failure'])

    def test_custom_analysis_uses_task_interpreter_and_keeps_receipt(self):
        context = {**self.context, 'artifact_dir': '/workspace/task/artifacts'}
        broker = Broker({}, context, {'required': [], 'not_applicable': []},
                        self.root / 'analysis', lambda: False)
        with patch.object(broker, 'command', return_value={'id': 'analysis', 'returncode': 0, 'termination': None}) as command:
            result = broker.invoke('custom_test', {'path': 'compare_ir.py', 'args': ['--baseline', 'base.json']})
        self.assertEqual(command.call_args.args[1]['argv'],
                         ['/trusted/task_python', '/workspace/task/artifacts/custom/compare_ir.py', '--baseline', 'base.json'])
        self.assertEqual(result['evidence'], ['analysis'])
        from tools.ai_custom_tools.runner import plan
        for parameters in ({'path': '../escape.py'}, {'path': '/outside.py'},
                           {'path': 'compare.py', 'timeout': 0}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                plan(context, parameters)

    @unittest.skipIf(os.name == 'nt', 'real bash syntax checks run in the persistent Linux worker and GitHub')
    def test_actual_syntax_checks_never_execute_candidate_programs(self):
        marker = self.root / 'must-not-exist'
        control_plane.syntax(f'touch "{marker}"\n', 'bash', 'safe-parse')
        control_plane.syntax(f'require("fs").writeFileSync({json.dumps(str(marker))}, "bad");\n', 'javascript', 'safe-parse')
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(ValueError, 'syntax failed'):
            control_plane.syntax('if true; then\n', 'bash', 'bad-bash')
        with self.assertRaisesRegex(ValueError, 'syntax failed'):
            control_plane.syntax('const = ;\n', 'javascript', 'bad-js')
        result = control_plane.check_router(self.root)
        self.assertEqual(result['workflow_count'], 4)
        self.assertEqual(result['script_count'], 8)
        self.assertEqual(len(result['sha256']), 4)


class ControlPolicyTests(unittest.TestCase):
    def test_control_and_documentation_need_no_compiler_build(self):
        paths = ['scripts/local_ci/runtime/cache.py', 'docs/pipeline.md', 'README.md']
        policy = minimum_checks(paths, {'triton_version': '3.0'})
        self.assertEqual(policy['required'], ['environment', 'control_plane', 'architecture_review'])

    def test_compiler_and_unknown_files_keep_the_conservative_floor(self):
        for changed in ('python/triton_anchor/example.py', 'unrecognized.cfg'):
            with self.subTest(changed=changed):
                policy = minimum_checks(['.github/workflows/ci.yml', 'README.md', changed], {'triton_version': '3.0'})
                self.assertTrue({'environment', 'frontend_build', 'frontend_install', 'frontend_smoke',
                                 'frontend_tests', 'control_plane', 'architecture_review'} <= set(policy['required']))
                if changed == 'unrecognized.cfg':
                    self.assertTrue(set(BACKEND_TOOLS) <= set(policy['required']))


if __name__ == '__main__':
    unittest.main()
