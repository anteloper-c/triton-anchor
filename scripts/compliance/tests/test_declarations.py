from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.compliance.declarations import declaration_delta, scan_declarations


class DeclarationInventoryTests(unittest.TestCase):
    def test_cmake_inventory_ignores_sources_and_internal_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "triton" / "third_party" / "f2reduce").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text(
                """
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
        self.assertEqual({"LLVM", "f2reduce", "pybind11", "zlib"}, names)
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
                '[build-system]\nrequires = ["setuptools>=68", "wheel"]\n',
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
        self.assertNotIn(("json", ("runtime-external",)), identities)

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
