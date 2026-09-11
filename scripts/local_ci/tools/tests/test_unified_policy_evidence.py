"""Regression checks for scope gaps and the common native/builtin report rules."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_ci import policy
from tools.basic_tools import actions, evidence, runner


class PolicyTests(unittest.TestCase):
    def classify(self, *paths, backend=True, full=False, **extra):
        return policy.minimum_checks(
            [{"path": path, "status": "M", **extra} for path in paths],
            backend_enabled=backend,
            full=full,
        )

    def test_test_only_executes_changed_tests(self):
        selected = self.classify("python/triton_anchor/tests/test_ir.py")
        self.assertIn("frontend_tests", selected["required_checks"])
        self.assertIn("frontend_install", selected["required_checks"])
        self.assertEqual(
            selected["required_parameters"]["frontend_tests"]["paths"],
            ["python/triton_anchor/tests/test_ir.py"],
        )
        self.assertNotIn("frontend_smoke", selected["required_checks"])

    def test_changed_smoke_script_is_executed_as_smoke(self):
        selected = self.classify("tests/test_smoke.py")
        self.assertIn("frontend_smoke", selected["required_checks"])
        self.assertNotIn("frontend_tests", selected["required_checks"])

    def test_deleted_tests_execute_remaining_suite(self):
        selected = self.classify("python/triton_anchor/tests/test_ir.py", status="D")
        self.assertIn("frontend_tests", selected["required_checks"])
        self.assertNotIn("frontend_tests", selected["required_parameters"])

    def test_ast_equivalence_never_waives_code_execution(self):
        selected = self.classify(
            "python/triton_anchor/compiler.py",
            semantic="python_ast_equivalent",
            old_mode="100644",
            mode="100644",
        )
        self.assertIn("frontend_tests", selected["required_checks"])
        self.assertEqual(selected["impact"]["classification"], "risk_assessed")

    def test_control_tests_use_control_plane_without_wheel_build(self):
        selected = self.classify("scripts/local_ci/agent_ci/tests/test_executor.py")
        self.assertIn("control_plane", selected["required_checks"])
        self.assertNotIn("frontend_build", selected["required_checks"])

    def test_full_enables_supported_full_operators(self):
        selected = self.classify("README.md", full=True)
        self.assertEqual(set(runner.TOOL_IDS), set(selected["required_checks"]))
        self.assertEqual(selected["required_parameters"]["flaggems"], {"mode": "full"})
        without = self.classify("README.md", full=True, backend=False)
        self.assertNotIn("flaggems", without["required_checks"])
        self.assertNotIn("flaggems", without["required_parameters"])

    def test_docs_are_lightweight_and_mixed_changes_union(self):
        self.assertEqual(
            self.classify("README.md")["required_checks"], ["control_plane"]
        )
        selected = self.classify(
            "scripts/local_ci/agent_ci/worker.py", "python/triton_anchor/pipeline.py"
        )
        for tool in ("control_plane", "frontend_tests", "backend_tests", "flaggems"):
            self.assertIn(tool, selected["required_checks"])

    def test_renamed_code_and_unknown_paths_keep_code_checks(self):
        renamed = policy.minimum_checks(
            [{"path": "docs/example.md", "old_path": "csrc/old.cpp", "status": "R100"}],
            backend_enabled=True,
        )
        self.assertTrue(
            {
                "frontend_tests",
                "backend_build",
                "backend_install",
                "backend_smoke",
                "flaggems",
            }
            <= set(renamed["required_checks"])
        )
        self.assertEqual(
            self.classify("unknown.cfg")["required_checks"], list(runner.TOOL_IDS)
        )
        with self.assertRaises(policy.ContractError):
            policy.minimum_checks([], backend_enabled=True)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = {
            "task_id": "fixture",
            "target_sha": "a" * 40,
            "environment_fingerprint": "environment-fixture",
        }

    def test_native_pytest_masked_failure_and_all_skip_never_pass(self):
        path = self.root / "test_actual.py"
        for source in (
            "def test_fail(): assert False\n",
            "import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n",
        ):
            path.write_text(source)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    str(path),
                    "--junitxml",
                    str(self.root / "tests.xml"),
                ],
                capture_output=True,
            )
            result = evidence.evaluate("frontend_tests", self.root, 0, self.context, {})
            self.assertEqual(result["status"], "fail")

    def test_builtin_and_native_use_the_same_real_junit(self):
        path = self.root / "test_actual.py"
        path.write_text("def test_add(): assert 1 + 1 == 2\n")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(path),
                "--junitxml",
                str(self.root / "tests.xml"),
            ],
            check=True,
            capture_output=True,
        )
        self.context["expected_imports"] = {
            name: {"path": "/task/venv/" + name, "sha256": "f" * 64}
            for name in ("triton", "triton_anchor")
        }
        actions.write_json(
            self.root / "import-origin.json",
            {
                **self.context,
                "status": "pass",
                "imports": self.context["expected_imports"],
            },
        )
        native = evidence.evaluate("frontend_tests", self.root, 0, self.context)
        self.assertEqual(native["status"], "pass")
        self.assertEqual(native["scope"]["observed_tests"][0]["name"], "test_add")
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 1, self.context)["status"],
            "fail",
        )
        summary = {
            **self.context,
            "junit_sha256": actions.digest(self.root / "tests.xml"),
        }
        actions.write_json(self.root / "tests.json", summary)
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "pass",
        )
        summary["target_sha"] = "b" * 40
        actions.write_json(self.root / "tests.json", summary)
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "infra_error",
        )

    def test_native_pytest_without_process_import_evidence_does_not_count(self):
        (self.root / "tests.xml").write_text(
            "<testsuite><testcase name='actual'/></testsuite>"
        )
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "infra_error",
        )

    def test_zero_exit_and_empty_report_are_not_coverage(self):
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "infra_error",
        )
        (self.root / "tests.xml").write_text("<testsuite tests='10'/>")
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "fail",
        )

    def test_immutable_dependency_wheel_is_reused_from_explicit_execution(self):
        built = self.root / "previous/frontend_build"
        (built / "wheels").mkdir(parents=True)
        wheel = built / "wheels/triton_anchor-fixture.whl"
        wheel.write_bytes(b"wheel")
        actions.write_json(
            built / "wheel.json",
            {**self.context, "wheel": str(wheel), "sha256": actions.digest(wheel)},
        )
        context = {
            **self.context,
            "artifact_dir": str(self.root / "next"),
            "dependency_artifacts": {"frontend_build": str(built)},
        }
        self.assertEqual(actions.wheel_manifest(context, "frontend_build")[1], wheel)
        context["environment_fingerprint"] = "changed"
        with self.assertRaisesRegex(ValueError, "different environment"):
            actions.wheel_manifest(context, "frontend_build")

    def test_frontend_import_origin_checks_distribution_and_ignores_diagnostics(self):
        site = self.root / "site"
        site.mkdir()
        for name in ("triton", "triton_anchor"):
            package = site / name
            package.mkdir()
            (package / "__init__.py").write_text(
                "import os\nprint('python diagnostic')\nos.write(1,b'native diagnostic\\n')\n"
            )
        metadata = site / "triton_anchor-0.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: triton-anchor\nVersion: 0.0\n")
        (metadata / "RECORD").write_text(
            "triton/__init__.py,,\ntriton_anchor/__init__.py,,\n"
        )

        def probe(argv, **kwargs):
            code = "import sys; sys.path.insert(0," + repr(str(site)) + ")\n" + argv[-1]
            return subprocess.run(
                [sys.executable, "-I", "-c", code], check=True, **kwargs
            )

        with patch.object(actions, "run", side_effect=probe):
            identity = actions.import_identity(
                {"python_bin": sys.executable, "artifact_dir": str(self.root)}
            )
        self.assertEqual(set(identity["imports"]), {"triton", "triton_anchor"})

    def test_pytest_observer_detects_source_shadowing_in_the_actual_process(self):
        site = self.root / "site"
        site.mkdir()
        expected = {}
        for name in ("triton", "triton_anchor"):
            package = site / name
            package.mkdir()
            source = package / "__init__.py"
            source.write_text("VALUE = 'wheel'\n")
            expected[name] = {"path": str(source), "sha256": actions.digest(source)}
        metadata = site / "triton_anchor-0.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: triton-anchor\nVersion: 0.0\n")
        (metadata / "RECORD").write_text(
            "triton/__init__.py,,\ntriton_anchor/__init__.py,,\n"
        )
        installation = self.root / "installation.json"
        actions.write_json(installation, {**self.context, "imports": expected})
        observer = ROOT / "tools/basic_tools/pytest_exec.py"
        tests = self.root / "test_process.py"
        report = self.root / "import-origin.json"
        argv = [
            str(observer),
            "--installation",
            str(installation),
            "--import-report",
            str(report),
            "--",
            "-q",
            str(tests),
            "--junitxml",
            str(self.root / "tests.xml"),
        ]
        program = (
            "import sys,runpy; sys.path.insert(0,"
            + repr(str(site))
            + "); sys.argv="
            + repr(argv)
            + "; runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        tests.write_text(
            "import triton_anchor\ndef test_installed(): assert triton_anchor.VALUE == 'wheel'\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.context["expected_imports"] = expected
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "pass",
        )
        shadow = self.root / "candidate/triton_anchor"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text("VALUE = 'source'\n")
        tests.write_text(
            "def test_source():\n import sys,importlib\n sys.path.insert(0,"
            + repr(str(shadow.parent))
            + ")\n del sys.modules['triton_anchor']\n import triton_anchor\n assert triton_anchor.VALUE == 'source'\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(report.read_text())["status"], "fail")
        self.assertEqual(
            evidence.evaluate("frontend_tests", self.root, 0, self.context)["status"],
            "infra_error",
        )

    def test_corrupt_candidate_measurements_block_but_missing_baseline_does_not(self):
        out = self.root / "compile_time"
        out.mkdir()
        context = {**self.context, "artifact_dir": str(self.root)}
        payload = {
            "tool_id": "compile_time",
            "context": context,
            "parameters": {},
            "kernels": ["add"],
        }
        actions.write_json(out / "candidate.json", {"summary": {}})
        with self.assertRaises(ValueError):
            actions.compare_performance(payload)
        actions.write_json(
            out / "candidate.json",
            {
                "summary": {
                    "add": {
                        "all_correct": True,
                        "compile_est": {"median_ms": 1, "count": 2},
                    }
                }
            },
        )
        actions.compare_performance(payload)
        self.assertEqual(
            actions.read_json(out / "comparison.json")["status"], "not_comparable"
        )


class PerformanceAssociationTests(unittest.TestCase):
    def test_performance_reports_require_current_measurement_identity_and_real_comparison(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            context = {
                "task_id": "fixture",
                "target_sha": "a" * 40,
                "base_sha": "b" * 40,
                "environment_fingerprint": "env",
            }
            candidate = {
                "metadata": {
                    "commit_sha": "a" * 40,
                    "environment_fingerprint": "env",
                    "kernels": ["add"],
                },
                "summary": {
                    "add": {
                        "all_correct": True,
                        "compile_est": {"median_ms": 2, "count": 1},
                    }
                },
            }
            comparison = {
                "status": "warning",
                "candidate_sha": "a" * 40,
                "base_sha": "b" * 40,
            }
            actions.write_json(root / "candidate.json", candidate)
            actions.write_json(root / "comparison.json", comparison)
            self.assertEqual(
                evidence.evaluate("compile_time", root, 0, context)["status"], "pass"
            )
            candidate["metadata"]["commit_sha"] = "c" * 40
            actions.write_json(root / "candidate.json", candidate)
            self.assertEqual(
                evidence.evaluate("compile_time", root, 0, context)["status"],
                "infra_error",
            )
            candidate["metadata"]["commit_sha"] = "a" * 40
            actions.write_json(root / "candidate.json", candidate)
            actions.write_json(root / "comparison.json", {})
            self.assertEqual(
                evidence.evaluate("compile_time", root, 0, context)["status"],
                "infra_error",
            )
            actions.write_json(
                root / "comparison.json",
                {"status": "not_comparable", "reason": "baseline_missing"},
            )
            self.assertEqual(
                evidence.evaluate("compile_time", root, 0, context)["status"], "pass"
            )
