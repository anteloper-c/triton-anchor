import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deterministic_ci/flaggems"))
import batch_test_flaggems as batch


class ReadOnlyFlagGemsTests(unittest.TestCase):
    def test_test_paths_resolve_from_mount_not_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            test = root / "tests/test_add.py"
            test.touch()
            selected = batch.SelectedOperator("math", "add", "add", ("tests/test_add.py::test_add",))
            command = batch.build_pytest_command(selected, sys.executable, "-q", root)
            self.assertIn(str(test) + "::test_add", command)
            empty = batch.SelectedOperator("math", "add", "add", ())
            self.assertIn(str(root / "tests"), batch.build_pytest_command(empty, sys.executable, "-q", root))
            escaped = batch.SelectedOperator("math", "add", "add", ("../other.py",))
            with self.assertRaises(ValueError):
                batch.build_pytest_command(escaped, sys.executable, "-q", root)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest needed for subprocess integration")
    def test_pytest_writes_report_and_caches_outside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            (tests / "test_add.py").write_text(
                "import json,os\nfrom pathlib import Path\n"
                "def test_add():\n"
                "    cwd=Path.cwd()\n"
                "    assert not cwd.is_relative_to(Path(os.environ['FLAGGEMS_ROOT']))\n"
                "    assert os.environ['PYTHONDONTWRITEBYTECODE']=='1'\n"
                "    assert Path(os.environ['FLAGGEMS_CACHE_DIR']).is_relative_to(cwd)\n"
                "    Path('result.json').write_text(json.dumps({'passed': True}))\n"
            )
            before = sorted(str(path.relative_to(source)) for path in source.rglob("*"))
            logs, dump = root / "logs", root / "dump"
            logs.mkdir()
            dump.mkdir()
            args = SimpleNamespace(python_bin=sys.executable, pytest_args="-q", mode="single", clear_cache="0",
                                   total_timeout_seconds=30, full_hard_timeout_seconds=30, idle_timeout_seconds=30)
            selected = batch.SelectedOperator("math", "add", "", ("tests/test_add.py",))
            result = batch.run_operator(selected, 1, args, source, dump, logs)
            self.assertEqual(0, result.exit_code, (logs / "001-add.log").read_text())
            self.assertEqual(1, result.passed)
            self.assertTrue(json.loads((logs / "001-add-work/result.json").read_text())["passed"])
            self.assertEqual(before, sorted(str(path.relative_to(source)) for path in source.rglob("*")))
