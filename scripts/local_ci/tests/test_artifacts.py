"""Real files prove evidence is retained, bounded and immutable across retries."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control.runtime.artifacts import collect_artifacts
from control.runtime.common import digest, write_json
from control.runtime.broker import Broker, DEPENDENCIES
from tools.basic_tools.runner import DEPENDENCIES as TOOL_DEPENDENCIES


class ArtifactTests(unittest.TestCase):
    def test_dependency_contract_has_one_source(self):
        self.assertIs(DEPENDENCIES, TOOL_DEPENDENCIES)
        self.assertEqual(DEPENDENCIES['backend_build'], ['environment'])
        self.assertEqual(DEPENDENCIES['backend_tests'], ['frontend_install', 'backend_install'])

    def test_test_evidence_must_match_real_cases_identity_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = {'artifact_host_dir': str(root / 'artifacts'), 'task_id': 'task-tests',
                       'target_sha': 'a' * 40, 'python_bin': '/opt/anchor-ci/control/runtime/task_python',
                       'task_venv': '/workspace/tasks/task-tests/run/venv'}
            broker = Broker({}, context, {}, root / 'output', lambda: False)
            for tool in ('frontend_tests', 'backend_tests'):
                output = root / 'artifacts' / tool
                output.mkdir(parents=True)
                junit = output / 'tests.xml'
                junit.write_text('<testsuites><testsuite><testcase name="actual-case"/></testsuite></testsuites>')
                valid = {key: context[key] for key in ('task_id', 'target_sha', 'task_venv')}
                valid.update(tool=tool, python_executable=context['python_bin'], junit_sha256=digest(junit),
                             tests=1, passed=1, failures=0, errors=0, skipped=0, selected_paths=['tests'])
                write_json(output / 'tests.json', valid)
                broker.verify_artifacts(tool)
                for field, bad in (('passed', 0), ('tests', True), ('target_sha', 'b' * 40),
                                   ('junit_sha256', 'c' * 64), ('tool', 'flaggems'),
                                   ('task_venv', '/workspace/tasks/other/run/venv'), ('selected_paths', [])):
                    with self.subTest(tool=tool, field=field), self.assertRaises(ValueError):
                        write_json(output / 'tests.json', {**valid, field: bad})
                        broker.verify_artifacts(tool)
                for outcome in ('skipped', 'failure', 'error'):
                    junit.write_text(f'<testsuites><testsuite><testcase><{outcome}/></testcase></testsuite></testsuites>')
                    with self.subTest(tool=tool, outcome=outcome), self.assertRaises(ValueError):
                        write_json(output / 'tests.json', {**valid, 'junit_sha256': digest(junit)})
                        broker.verify_artifacts(tool)

    def test_backend_build_is_independent_of_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / 'artifacts'
            wheel = artifacts / 'backend_build/wheels/backend.whl'
            wheel.parent.mkdir(parents=True)
            wheel.write_bytes(b'synthetic wheel bytes for host artifact validation')
            context = {'artifact_host_dir': str(artifacts), 'artifact_dir': '/artifacts',
                       'task_id': 'task-build', 'target_sha': 'a' * 40}
            write_json(wheel.parent.parent / 'wheel.json',
                       {'task_id': context['task_id'], 'target_sha': context['target_sha'],
                        'wheel': '/artifacts/backend_build/wheels/backend.whl', 'sha256': digest(wheel)})
            broker = Broker({}, context, {}, root / 'output', lambda: False)
            broker.verify_artifacts('backend_build')
            with self.assertRaisesRegex(ValueError, 'without its required artifact'):
                broker.verify_artifacts('backend_install')

    def test_zero_exit_without_required_build_artifact_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker({}, {'artifact_host_dir': str(root/'artifacts')}, {}, root/'output', lambda:False)
            with self.assertRaisesRegex(ValueError, 'without its required artifact'):
                broker.verify_artifacts('frontend_build')

    def test_preserves_tests_and_ir_while_reporting_omissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'source', root / 'output'
            (source / 'custom').mkdir(parents=True)
            (source / 'custom/repro.py').write_text('assert 1 == 1\n')
            (source / 'result.mlir').write_text('module {}\n')
            (source / 'wheel.whl').write_bytes(b'not-published')
            (source / 'oversize.log').write_text('x' * 256)
            (source / '.auth.json').write_text('{}')
            manifest = collect_artifacts(source, output, max_file_bytes=128)
            self.assertEqual({x['path'] for x in manifest['files']},
                             {'artifacts/custom/repro.py', 'artifacts/result.mlir'})
            self.assertEqual(len(manifest['omitted']), 3)
            for item in manifest['files']:
                self.assertEqual(digest(output / item['path']), item['sha256'])
            (source / 'custom/repro.py').write_text('later mutation')
            self.assertEqual((output / 'artifacts/custom/repro.py').read_text(), 'assert 1 == 1\n')

    def test_total_budget_reports_remaining_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name in ('a.txt', 'b.txt'):
                (source / name).write_text('0123456789')
            manifest = collect_artifacts(source, root / 'output', max_total_bytes=10)
            self.assertEqual(len(manifest['files']), 1)
            self.assertEqual(manifest['omitted'][0]['reason'], 'total publication limit')
