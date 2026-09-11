"""Minimum behavior coverage from documentation, code, tests and mixed diffs."""
import unittest

from agent_ci import policy
from tools.basic_tools import runner


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

    def test_control_tests_use_control_plane_without_wheel_build(self):
        selected = self.classify("scripts/local_ci/tests/test_worker.py")
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
