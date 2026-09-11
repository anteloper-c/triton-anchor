"""Main-like docs/router repositories receive real contracts without test dirs."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "independent_contract_checks", TOOLS / "basic_tools/contract_checks.py"
)
contracts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contracts)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-main-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "main"
        self.checkout.mkdir()
        self.git("init", "-q")
        (self.checkout / "README.md").write_text("# CI\n")
        self.base = self.commit()

    def git(self, *args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "user.name=Contract Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    *args,
                ],
                cwd=self.checkout,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def change(self, path, content):
        target = self.checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return self.commit()

    def invoke(self, sha):
        context = {
            "source_dir": str(self.checkout),
            "artifact_dir": str(self.root / "artifacts"),
            "task_id": "contract-fixture",
            "target_sha": sha,
            "base_sha": self.base,
            "triton_version": "3.0",
            "python_bin": sys.executable,
            "trusted_python_bin": sys.executable,
            "tools_dir": str(TOOLS),
        }
        path = self.root / "context.json"
        path.write_text(json.dumps(context))
        return subprocess.run(
            [
                sys.executable,
                str(TOOLS / "basic_tools/runner.py"),
                "control_plane",
                "--context",
                str(path),
                "--execute",
            ],
            capture_output=True,
            text=True,
        )

    def test_docs_main_without_scripts_runs_real_contract(self):
        sha = self.change(
            "README.md", "# CI\n\nDispatch runs against a frozen commit.\n"
        )
        process = self.invoke(sha)
        self.assertEqual(0, process.returncode, process.stderr)
        result = json.loads(
            (self.root / "artifacts/control_plane/control_plane.json").read_text()
        )
        self.assertEqual([], result["regression_paths"])
        self.assertEqual(
            ["utf8", "no_conflict_markers"], result["verified_files"][0]["checks"]
        )
        self.assertEqual(self.base, result["base_sha"])

    def test_router_main_has_workflow_shape_contract(self):
        sha = self.change(
            ".github/workflows/router.yml",
            "name: Router\non: workflow_dispatch\njobs:\n  dispatch:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo fixture\n",
        )
        result = contracts.check(self.checkout, self.base, sha)
        self.assertIn("workflow_contract", result["verified_files"][0]["checks"])

    def test_bad_yaml_and_empty_workflow_fail(self):
        sha = self.change(
            ".github/workflows/router.yml", "on: [workflow_dispatch\njobs: {}\n"
        )
        self.assertNotEqual(0, self.invoke(sha).returncode)
        sha = self.change(".github/workflows/router.yml", "{}\n")
        self.assertNotEqual(0, self.invoke(sha).returncode)

    def test_missing_job_steps_and_unknown_needs_fail(self):
        sha = self.change(
            ".github/workflows/router.yml",
            "on: workflow_dispatch\njobs:\n  dispatch:\n    runs-on: ubuntu-latest\n    steps: []\n",
        )
        with self.assertRaisesRegex(ValueError, "nonempty steps"):
            contracts.check(self.checkout, self.base, sha)
        sha = self.change(
            ".github/workflows/router.yml",
            "on: workflow_dispatch\njobs:\n  dispatch:\n    needs: missing\n    uses: example/repo/.github/workflows/run.yml@main\n",
        )
        with self.assertRaisesRegex(ValueError, "needs"):
            contracts.check(self.checkout, self.base, sha)

    def test_conflicts_and_python_syntax_fail(self):
        sha = self.change("README.md", "<<<<<<< HEAD\nbroken\n>>>>>>> other\n")
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            contracts.check(self.checkout, self.base, sha)
        (self.checkout / "README.md").write_text("# valid\n")
        sha = self.change("scripts/check.py", "def broken(\n")
        with self.assertRaises(SyntaxError):
            contracts.check(self.checkout, self.base, sha)

    def test_empty_change_is_not_a_pass(self):
        with self.assertRaisesRegex(ValueError, "no changed files"):
            contracts.check(self.checkout, self.base, self.base)


if __name__ == "__main__":
    unittest.main()
