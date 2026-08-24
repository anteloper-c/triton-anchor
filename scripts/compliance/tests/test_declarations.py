from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.compliance.core import reconcile_discoveries
from scripts.compliance.declarations import declaration_delta, main, scan_declarations
from scripts.compliance.tests.helpers import component


class DeclarationInventoryTests(unittest.TestCase):
    def test_cli_writes_one_source_tree_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[build-system]\nrequires = ["setuptools"]\n', encoding="utf-8"
            )
            output = root / "inventory.json"

            code = main(["--root", str(root), "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("dependency-inventory", report["source"])
        self.assertEqual("setuptools", report["items"][0]["name"])

    def test_cmake_inventory_ignores_sources_and_internal_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "triton" / "third_party" / "f2reduce").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text(
                """
                cmake_minimum_required(VERSION 3.18)
                find_package(MLIR REQUIRED CONFIG)
                find_package(pybind11 CONFIG REQUIRED)
                add_triton_library(f2reduce f2reduce.cpp)
                add_triton_library(TritonTools
                  LinearLayout.cpp
                  DEPENDS
                  LINK_LIBS PUBLIC MLIRIR f2reduce
                )
                target_link_libraries(triton PRIVATE pybind11::headers z)
                target_link_libraries(triton PRIVATE "-undefined dynamic_lookup -flto")
                """,
                encoding="utf-8",
            )

            report = scan_declarations(root)

        self.assertEqual("success", report["status"], report)
        names = {item["name"] for item in report["items"]}
        self.assertEqual({"CMake", "LLVM", "f2reduce", "pybind11", "zlib"}, names)
        self.assertNotIn("LinearLayout.cpp", names)
        self.assertNotIn("dynamic_lookup", names)

    def test_python_and_pyproject_dependencies_have_semantic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "python" / "demo"
            package.mkdir(parents=True)
            (package / "module.py").write_text(
                "import json\nimport numpy as np\nfrom redis.client import Redis\n",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                '[build-system]\nrequires = ["setuptools>=68", "wheel"]\n'
                '[project]\nname = "demo"\ndependencies = ["requests>=2"]\n'
                '[project.optional-dependencies]\ndev = ["ruff"]\n',
                encoding="utf-8",
            )
            (root / "setup.py").write_text(
                'cmake_args = ["-G", "Ninja"]\n'
                'setup(python_requires=">=3.8", install_requires=["packaging>=23"])\n',
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.18)\n",
                encoding="utf-8",
            )

            report = scan_declarations(root)

        identities = {
            (item["name"], tuple(item["candidate_usages"]))
            for item in report["items"]
        }
        self.assertIn(("numpy", ("runtime-external",)), identities)
        self.assertIn(("redis", ("runtime-external",)), identities)
        self.assertIn(("setuptools", ("build-only",)), identities)
        self.assertIn(("wheel", ("build-only",)), identities)
        self.assertIn(("Ninja", ("build-only",)), identities)
        self.assertIn(("Python3", ("runtime-external",)), identities)
        self.assertIn(("packaging", ("runtime-external",)), identities)
        self.assertIn(("requests", ("runtime-external",)), identities)
        self.assertIn(("ruff", ("runtime-external",)), identities)
        self.assertNotIn(("json", ("runtime-external",)), identities)

        cmake = component("cmake", ">=99", distribution="build-only")
        cmake["name"] = "CMake"
        cmake["usages"][0]["target"] = "source-snapshot"
        cpython = component("cpython", ">=99", distribution="runtime-external")
        cpython["aliases"] = ["Python3"]
        cpython["usages"][0]["target"] = "source-snapshot"
        reconciliation = reconcile_discoveries(
            {"schema_version": 1, "components": [cmake, cpython]},
            [
                {
                    "source": "dependency-inventory",
                    "status": "success",
                    "issues": [],
                    "items": [
                        item
                        for item in report["items"]
                        if item["name"] in {"CMake", "Python3"}
                    ],
                }
            ],
            target="source-snapshot",
        )
        self.assertEqual(
            2,
            sum(
                issue.get("code") == "declaration-constraint-mismatch"
                for issue in reconciliation["execution_issues"]
            ),
        )

    def test_gitmodule_inventory_retains_and_checks_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitmodules").write_text(
                '[submodule "FlagGems"]\n'
                "\tpath = FlagGems\n"
                "\turl = https://example.test/other/FlagGems.git\n",
                encoding="utf-8",
            )
            report = scan_declarations(root)

        declaration = report["items"][0]
        self.assertEqual("FlagGems", declaration["submodule_path"])
        self.assertEqual(
            "https://example.test/other/FlagGems.git",
            declaration["origin_url"],
        )

        flaggems = component("flaggems", "1" * 40, distribution="test-only")
        flaggems["name"] = "FlagGems"
        flaggems["origin"] = {
            "url": "https://github.com/RACE-org/FlagGems",
            "status": "resolved",
        }
        flaggems["usages"][0]["target"] = "source-snapshot"
        reconciliation = reconcile_discoveries(
            {"schema_version": 1, "components": [flaggems]},
            [report],
            target="source-snapshot",
        )
        self.assertIn(
            "submodule-origin-mismatch",
            {issue.get("code") for issue in reconciliation["execution_issues"]},
        )

        matching_origin = {
            **report,
            "items": [
                {
                    **declaration,
                    "declaration_value": "https://github.com/RACE-org/FlagGems",
                    "origin_url": "https://github.com/RACE-org/FlagGems",
                    "submodule_path": "other/FlagGems",
                }
            ],
        }
        gitlink = {
            "source": "source-snapshot",
            "status": "success",
            "issues": [],
            "items": [
                {
                    "kind": "dependency-declaration",
                    "name": "FlagGems",
                    "path": "FlagGems",
                    "version": "1" * 40,
                    "candidate_usages": ["test-only"],
                    "declaration_source": "gitlink",
                }
            ],
        }
        path_mismatch = reconcile_discoveries(
            {"schema_version": 1, "components": [flaggems]},
            [matching_origin, gitlink],
            target="source-snapshot",
        )
        self.assertIn(
            "submodule-path-mismatch",
            {issue.get("code") for issue in path_mismatch["execution_issues"]},
        )

        changed = {
            **report,
            "items": [
                {
                    **declaration,
                    "declaration_value": "https://github.com/RACE-org/FlagGems",
                    "origin_url": "https://github.com/RACE-org/FlagGems",
                }
            ],
        }
        self.assertEqual(1, len(declaration_delta(report, changed)["items"]))

    def test_delta_retains_all_evidence_locations_for_new_dependency(self) -> None:
        baseline = {
            "source": "dependency-inventory",
            "status": "success",
            "items": [],
            "issues": [],
        }
        current = {
            "source": "dependency-inventory",
            "status": "success",
            "items": [
                {
                    "kind": "dependency-declaration",
                    "name": "example",
                    "path": "one.py",
                    "candidate_usages": ["runtime-external"],
                },
                {
                    "kind": "dependency-declaration",
                    "name": "example",
                    "path": "two.py",
                    "candidate_usages": ["runtime-external"],
                },
            ],
            "issues": [],
        }

        delta = declaration_delta(baseline, current)

        self.assertEqual("success", delta["status"])
        self.assertEqual(2, len(delta["items"]))
        self.assertEqual({"one.py", "two.py"}, {item["path"] for item in delta["items"]})

    def test_delta_detects_build_requirement_version_change(self) -> None:
        baseline = {
            "source": "dependency-inventory",
            "status": "success",
            "items": [
                {
                    "kind": "dependency-declaration",
                    "name": "setuptools",
                    "path": "pyproject.toml",
                    "candidate_usages": ["build-only"],
                    "declaration_value": "setuptools>=64",
                }
            ],
            "issues": [],
        }
        current = {
            **baseline,
            "items": [
                {
                    **baseline["items"][0],
                    "declaration_value": "setuptools>=68",
                }
            ],
        }

        delta = declaration_delta(baseline, current)

        self.assertEqual("success", delta["status"])
        self.assertEqual("setuptools>=68", delta["items"][0]["declaration_value"])


if __name__ == "__main__":
    unittest.main()
