from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "api_contract" / "check_public_api.py"
SPEC = importlib.util.spec_from_file_location("check_public_api", CHECKER_PATH)
CHECKER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CHECKER)


class PublicApiCheckerTests(unittest.TestCase):
    def test_current_repository_matches_its_contract(self):
        scope = REPO_ROOT / "api_contract" / "public_api.json"
        result = CHECKER.run_check(REPO_ROOT, REPO_ROOT, scope, scope)
        self.assertEqual("compatible", result["status"])
        self.assertEqual(0, result["breaking_count"])

    def test_removed_method_is_breaking(self):
        result = self._compare(
            "class Service:\n    def run(self, value: int = 1):\n        return value\n",
            "class Service:\n    pass\n",
            classes={"Service": {"kind": "class", "methods": ["run"]}},
        )
        self.assert_change(result, "method-removed")

    def test_required_parameter_is_breaking(self):
        result = self._compare(
            "def compile(source, debug=False):\n    pass\n",
            "def compile(source, target, debug=False):\n    pass\n",
            functions=["compile"],
        )
        self.assert_change(result, "required-parameter-added")

    def test_appended_optional_parameter_is_compatible(self):
        result = self._compare(
            "def compile(source, debug=False):\n    pass\n",
            "def compile(source, debug=False, profile=False):\n    pass\n",
            functions=["compile"],
        )
        self.assertEqual("compatible", result["status"])
        self.assert_change(result, "optional-parameter-added")

    def test_enum_value_change_is_breaking(self):
        result = self._compare(
            "from enum import Enum\nclass Mode(Enum):\n    FAST = 'fast'\n",
            "from enum import Enum\nclass Mode(Enum):\n    FAST = 'quick'\n",
            classes={"Mode": {"kind": "enum"}},
        )
        self.assert_change(result, "enum-value-changed")

    def test_new_abstract_adapter_method_is_breaking(self):
        result = self._compare(
            "from abc import ABC, abstractmethod\nclass Adapter(ABC):\n    @abstractmethod\n    def run(self):\n        pass\n",
            "from abc import ABC, abstractmethod\nclass Adapter(ABC):\n    @abstractmethod\n    def run(self):\n        pass\n    @abstractmethod\n    def reset(self):\n        pass\n",
            classes={"Adapter": {"kind": "class", "track_added_abstract_methods": True}},
        )
        self.assert_change(result, "abstract-method-added")

    def test_candidate_scope_change_is_only_a_warning_for_current_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            candidate = root / "candidate"
            self._write_module(base, "def stable():\n    pass\n")
            self._write_module(candidate, "def stable():\n    pass\n")
            base_scope = root / "base-scope.json"
            candidate_scope = root / "candidate-scope.json"
            self._write_scope(base_scope, functions=["stable"])
            self._write_scope(candidate_scope, functions=[])

            result = CHECKER.run_check(base, candidate, base_scope, candidate_scope)

        self.assertEqual("compatible", result["status"])
        self.assert_change(result, "scope-file-changed")

    def test_removed_candidate_scope_is_breaking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            candidate = root / "candidate"
            self._write_module(base, "def stable():\n    pass\n")
            self._write_module(candidate, "def stable():\n    pass\n")
            base_scope = root / "base-scope.json"
            self._write_scope(base_scope, functions=["stable"])

            result = CHECKER.run_check(
                base,
                candidate,
                base_scope,
                root / "missing-candidate-scope.json",
            )

        self.assertEqual("breaking", result["status"])
        self.assert_change(result, "scope-file-removed")

    def test_cli_writes_report_before_returning_breaking_exit_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            candidate = root / "candidate"
            scope = root / "scope.json"
            json_output = root / "result.json"
            markdown_output = root / "report.md"
            self._write_module(base, "def stable(value=1):\n    pass\n")
            self._write_module(candidate, "def stable():\n    pass\n")
            self._write_scope(scope, functions=["stable"])

            exit_code = CHECKER.main([
                "--base-root", str(base),
                "--candidate-root", str(candidate),
                "--scope", str(scope),
                "--candidate-scope", str(scope),
                "--json-output", str(json_output),
                "--markdown-output", str(markdown_output),
            ])

            result = json.loads(json_output.read_text(encoding="utf-8"))
            report = markdown_output.read_text(encoding="utf-8")

        self.assertEqual(CHECKER.BREAKING_EXIT_CODE, exit_code)
        self.assertEqual("breaking", result["status"])
        self.assertIn("Breaking changes detected", report)

    def _compare(self, base_source, candidate_source, functions=None, classes=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            candidate = root / "candidate"
            scope = root / "scope.json"
            self._write_module(base, base_source)
            self._write_module(candidate, candidate_source)
            self._write_scope(scope, functions=functions or [], classes=classes or {})
            return CHECKER.run_check(base, candidate, scope, scope)

    @staticmethod
    def _write_module(root, source):
        module = root / "python" / "sample.py"
        module.parent.mkdir(parents=True)
        module.write_text(source, encoding="utf-8")

    @staticmethod
    def _write_scope(path, functions, classes=None):
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "modules": {
                    "sample": {
                        "functions": functions,
                        "classes": classes or {},
                    }
                },
            }),
            encoding="utf-8",
        )

    def assert_change(self, result, code):
        self.assertIn(code, {change["code"] for change in result["changes"]})


if __name__ == "__main__":
    unittest.main()
