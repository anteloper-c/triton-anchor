"""Real files prove evidence is retained, bounded and immutable across retries."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.artifacts import collect_artifacts
from runtime.common import digest
from runtime.broker import Broker


class ArtifactTests(unittest.TestCase):
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
