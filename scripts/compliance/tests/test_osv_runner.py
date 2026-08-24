from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.compliance.core import normalize_osv
from scripts.compliance.osv_runner import (
    _query_admission_inventory,
    _query_inventory,
    _query_source_inventory,
    run_osv_admission_scan,
    run_osv_scan,
    run_osv_source_scan,
)
from scripts.compliance.tests.helpers import component


def registry_and_build() -> tuple[dict, dict]:
    dependency = component("setuptools", version=">=64", distribution="build-only")
    dependency["purl"] = "pkg:pypi/setuptools"
    dependency["version"] = {
        "value": ">=64",
        "kind": "version-constraint",
        "status": "constraint-only",
    }
    registry = {"schema_version": 1, "components": [dependency]}
    build = {
        "compliance_build": {
            "status": "success",
            "artifact_sha256": "a" * 64,
            "components": [
                {
                    "id": "setuptools",
                    "version": "68.1.2",
                    "usages": ["build-only"],
                    "evidence": {"source": "python-distribution"},
                }
            ],
        }
    }
    return registry, build


def raw_report(*, ecosystem: str = "PyPI", groups: list[dict] | None = None) -> bytes:
    value = {
        "results": [
            {
                "packages": [
                    {
                        "package": {
                            "name": "setuptools",
                            "version": "68.1.2",
                            "ecosystem": ecosystem,
                        },
                        "groups": groups or [],
                    }
                ]
            }
        ]
    }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def admission_registries() -> tuple[dict, dict]:
    baseline_dependency = component(
        "setuptools", version="67.0.0", distribution="build-only"
    )
    baseline_dependency["purl"] = "pkg:pypi/setuptools"
    current_dependency = component(
        "setuptools", version="68.1.2", distribution="build-only"
    )
    current_dependency["purl"] = "pkg:pypi/setuptools"
    baseline_unchanged = component("numpy", version="2.2.0")
    baseline_unchanged["purl"] = "pkg:pypi/numpy"
    current_unchanged = component("numpy", version="2.2.0")
    current_unchanged["purl"] = "pkg:pypi/numpy"
    baseline_git = component("triton", version="1" * 40)
    baseline_git["version"]["kind"] = "git-commit"
    baseline_git["origin"] = {
        "url": "https://github.com/triton-lang/triton",
        "status": "confirmed",
    }
    current_git = component("triton", version="2" * 40)
    current_git["version"]["kind"] = "git-commit"
    current_git["origin"] = {
        "url": "https://github.com/triton-lang/triton",
        "status": "confirmed",
    }
    return (
        {
            "schema_version": 1,
            "components": [baseline_dependency, baseline_unchanged, baseline_git],
        },
        {
            "schema_version": 1,
            "components": [current_dependency, current_unchanged, current_git],
        },
    )


def completed_runs(scan_exit: int, stdout: bytes) -> list[subprocess.CompletedProcess]:
    return [
        subprocess.CompletedProcess(
            ["osv-scanner", "--version"],
            0,
            stdout="osv-scanner version: 2.5.1\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            ["osv-scanner", "scan"], scan_exit, stdout=stdout, stderr=b""
        ),
    ]


class OsvRunnerTests(unittest.TestCase):
    def test_admission_query_contains_only_exact_changed_components(self) -> None:
        baseline, current = admission_registries()

        query, index = _query_admission_inventory(
            baseline, current, "core-wheel"
        )

        self.assertEqual({"setuptools", "triton"}, set(index))
        self.assertEqual(
            {
                ("setuptools", "68.1.2", None),
                ("github.com/triton-lang/triton", None, "2" * 40),
            },
            {
                (
                    item["package"]["name"],
                    item["package"].get("version"),
                    item["package"].get("commit"),
                )
                for item in query["results"][0]["packages"]
            },
        )

    def test_admission_scan_records_changed_component_coverage(self) -> None:
        baseline, current = admission_registries()
        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / "admission.raw.json"
            output_path = Path(temporary) / "admission.json"
            with patch(
                "scripts.compliance.osv_runner.subprocess.run",
                side_effect=completed_runs(0, raw_report()),
            ) as mocked:
                code = run_osv_admission_scan(
                    scanner="osv-scanner",
                    baseline_registry=baseline,
                    current_registry=current,
                    raw_output=raw_path,
                    output=output_path,
                    scanned_on="2026-08-24",
                )
            output = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        coverage = {
            item["component_id"]: item["component_version"]
            for item in output["coverage"]
        }
        self.assertEqual({"setuptools", "triton"}, set(coverage))
        self.assertEqual("68.1.2", coverage["setuptools"])
        self.assertEqual("2" * 40, coverage["triton"])
        self.assertIn("--lockfile", mocked.call_args_list[1].args[0])

        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "not-applicable.json"
            with patch("scripts.compliance.osv_runner.subprocess.run") as unused:
                code = run_osv_admission_scan(
                    scanner="osv-scanner",
                    baseline_registry=baseline,
                    current_registry=current,
                    raw_output=Path(temporary) / "not-applicable.raw.json",
                    output=output_path,
                    target="source-snapshot",
                    scanned_on="2026-08-24",
                )
            not_applicable = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("not-applicable", not_applicable["scanner_execution"]["status"])
        unused.assert_not_called()

    def _source_inputs(self) -> tuple[dict, dict, dict]:
        root = component("triton-anchor")
        root["third_party"] = False
        root["usages"] = [
            {
                "category": "distributed",
                "status": "confirmed",
                "target": "source-snapshot",
                "path_patterns": ["README.md"],
                "evidence_ids": [],
            }
        ]
        dependency = component("triton", distribution="distributed")
        dependency["origin"] = {
            "url": "https://github.com/triton-lang/triton",
            "status": "resolved",
        }
        dependency["version"] = {
            "value": "1" * 40,
            "kind": "git-commit",
            "status": "resolved",
        }
        dependency["usages"] = [
            {
                "category": "distributed",
                "status": "confirmed",
                "target": "source-snapshot",
                "path_patterns": ["triton/**"],
                "evidence_ids": [],
            }
        ]
        submodule = component("flaggems", distribution="test-only")
        submodule["name"] = "FlagGems"
        submodule["origin"] = {
            "url": "https://github.com/RACE-org/FlagGems",
            "status": "resolved",
        }
        submodule["version"] = {
            "value": "3" * 40,
            "kind": "git-commit",
            "status": "resolved-gitlink",
        }
        submodule["usages"][0]["target"] = "*"
        ci_only = component("workflow-action", distribution="CI-only")
        ci_only["version"] = {
            "value": "4" * 40,
            "kind": "git-commit",
            "status": "resolved",
        }
        ci_only["usages"][0].update(
            {"target": "*", "path_patterns": [".github/**"]}
        )
        root["usages"][0]["path_patterns"].append(".gitmodules")
        registry = {
            "schema_version": 1,
            "components": [root, dependency, submodule, ci_only],
        }
        source_artifact = {
            "artifact_kind": "github-source-snapshot",
            "files": [
                {"path": "README.md", "size": 1, "sha256": "2" * 64},
                {"path": "triton/a.cc", "size": 1, "sha256": "3" * 64},
                {"path": ".gitmodules", "size": 1, "sha256": "4" * 64},
                {
                    "path": ".github/workflows/ci.yml",
                    "size": 1,
                    "sha256": "5" * 64,
                },
            ],
            "gitlinks": [
                {
                    "path": "FlagGems",
                    "mode": "160000",
                    "object_type": "commit",
                    "commit": "3" * 40,
                }
            ],
        }
        inventory = {
            "source": "dependency-inventory",
            "status": "success",
            "issues": [],
            "items": [
                {
                    "kind": "dependency-declaration",
                    "name": "FlagGems",
                    "path": ".gitmodules",
                    "candidate_usages": ["test-only"],
                    "declaration_source": "git-submodule",
                    "declaration_value": "https://github.com/RACE-org/FlagGems",
                    "submodule_path": "FlagGems",
                    "origin_url": "https://github.com/RACE-org/FlagGems",
                }
            ],
        }
        return registry, source_artifact, inventory

    def test_source_query_uses_observed_exact_git_commit(self) -> None:
        registry, source_artifact, inventory = self._source_inputs()

        query, index = _query_source_inventory(
            registry, source_artifact, inventory, "source-snapshot"
        )

        self.assertEqual({"flaggems", "triton"}, set(index))
        self.assertEqual(
            {
                ("github.com/race-org/flaggems", "3" * 40),
                ("github.com/triton-lang/triton", "1" * 40),
            },
            {
                (item["package"]["name"], item["package"]["commit"])
                for item in query["results"][0]["packages"]
            },
        )

    def test_source_scan_records_commit_coverage(self) -> None:
        registry, source_artifact, inventory = self._source_inputs()
        raw = b'{"results":[]}'
        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / "source.raw.json"
            output_path = Path(temporary) / "source.json"
            with patch(
                "scripts.compliance.osv_runner.subprocess.run",
                side_effect=completed_runs(0, raw),
            ) as mocked:
                code = run_osv_source_scan(
                    scanner="osv-scanner",
                    registry=registry,
                    source_artifact=source_artifact,
                    dependency_inventory=inventory,
                    raw_output=raw_path,
                    output=output_path,
                    scanned_on="2026-08-24",
                )
            output = json.loads(output_path.read_text(encoding="utf-8"))
            normalized = normalize_osv(output, registry["components"])

        self.assertEqual(0, code)
        coverage = {
            item["component_id"]: item["component_version"]
            for item in output["coverage"]
        }
        self.assertEqual({"flaggems", "triton"}, set(coverage))
        self.assertEqual("1" * 40, coverage["triton"])
        self.assertEqual("3" * 40, coverage["flaggems"])
        self.assertEqual("success", normalized["status"], normalized)
        scan_argv = mocked.call_args_list[1].args[0]
        self.assertIn("--lockfile", scan_argv)
        self.assertNotIn("--all-packages", scan_argv)

    def _run(
        self, scan_exit: int, stdout: bytes
    ) -> tuple[int, bytes, dict, list]:
        registry, build = registry_and_build()
        with tempfile.TemporaryDirectory() as temporary:
            raw_path = Path(temporary) / "osv-scanner.raw.json"
            output_path = Path(temporary) / "osv-results.json"
            with patch(
                "scripts.compliance.osv_runner.subprocess.run",
                side_effect=completed_runs(scan_exit, stdout),
            ) as mocked:
                code = run_osv_scan(
                    scanner="osv-scanner",
                    registry=registry,
                    build_evidence=build,
                    raw_output=raw_path,
                    output=output_path,
                    scanned_on="2026-08-20",
                )
            raw_bytes = raw_path.read_bytes() if raw_path.exists() else b""
            output = json.loads(output_path.read_text(encoding="utf-8"))
            return code, raw_bytes, output, mocked.call_args_list

    def test_exit_one_is_successful_scan_with_findings_and_coverage(self) -> None:
        raw = raw_report(
            groups=[
                {
                    "ids": ["PYSEC-TEST"],
                    "aliases": ["CVE-TEST"],
                    "max_severity": "8.8",
                }
            ]
        )
        code, preserved, output, calls = self._run(1, raw)

        self.assertEqual(0, code)
        self.assertEqual(raw, preserved)
        self.assertEqual(
            "pkg:pypi/setuptools",
            output["results"][0]["packages"][0]["package"]["purl"],
        )
        self.assertEqual("68.1.2", output["coverage"][0]["component_version"])
        self.assertEqual("2.5.1", output["coverage"][0]["scanner_version"])
        self.assertEqual(
            f"osv-scanner.raw.json#sha256:{hashlib.sha256(raw).hexdigest()}",
            output["coverage"][0]["evidence"],
        )
        scan_argv = calls[1].args[0]
        self.assertIn("--all-packages", scan_argv)
        self.assertEqual(1, output["scanner_execution"]["exit_code"])

    def test_exit_zero_with_no_findings_still_proves_coverage(self) -> None:
        code, _, output, _ = self._run(0, raw_report())

        self.assertEqual(0, code)
        self.assertEqual([], output["results"][0]["packages"][0]["groups"])
        self.assertEqual("scanned", output["coverage"][0]["status"])

    def test_non_scanner_success_exit_fails_without_coverage(self) -> None:
        code, preserved, output, _ = self._run(128, b"")

        self.assertEqual(1, code)
        self.assertEqual(b"", preserved)
        self.assertIsNone(output["results"])
        self.assertEqual([], output["coverage"])
        self.assertEqual("failed", output["scanner_execution"]["status"])

    def test_invalid_json_fails_without_coverage(self) -> None:
        code, preserved, output, _ = self._run(0, b"not-json")

        self.assertEqual(1, code)
        self.assertEqual(b"not-json", preserved)
        self.assertIsNone(output["results"])
        self.assertEqual([], output["coverage"])

    def test_empty_ecosystem_fails_exact_query_validation(self) -> None:
        code, _, output, _ = self._run(0, raw_report(ecosystem=""))

        self.assertEqual(1, code)
        self.assertEqual([], output["coverage"])
        self.assertIsNone(output["results"])

    def test_wrong_ecosystem_fails_exact_query_validation(self) -> None:
        code, _, output, _ = self._run(0, raw_report(ecosystem="npm"))

        self.assertEqual(1, code)
        self.assertEqual([], output["coverage"])
        self.assertIsNone(output["results"])

    def test_exit_one_without_a_vulnerability_fails(self) -> None:
        code, _, output, _ = self._run(1, raw_report())

        self.assertEqual(1, code)
        self.assertIsNone(output["results"])
        self.assertEqual([], output["coverage"])

    def test_exit_zero_with_a_vulnerability_fails(self) -> None:
        code, _, output, _ = self._run(
            0,
            raw_report(
                groups=[{"ids": ["PYSEC-TEST"], "max_severity": "8.8"}]
            ),
        )

        self.assertEqual(1, code)
        self.assertIsNone(output["results"])
        self.assertEqual([], output["coverage"])

    def test_query_inventory_excludes_unresolved_abi_only_component(self) -> None:
        registry, build = registry_and_build()
        runtime = component("zstd", distribution="runtime-external")
        runtime["purl"] = "pkg:generic/zstd"
        runtime["runtime_constraint"] = "libzstd.so.1"
        runtime["version"] = {
            "value": None,
            "kind": "runtime-constraint",
            "status": "candidate-evidence-required",
        }
        registry["components"].append(runtime)
        build["compliance_build"]["components"].append(
            {
                "id": "zstd",
                "version": None,
                "usages": ["runtime-external"],
                "evidence": {"source": "readelf-dt-needed"},
            }
        )

        query, index = _query_inventory(registry, build, "core-wheel")

        self.assertEqual({"setuptools"}, set(index))
        self.assertEqual(
            ["pkg:pypi/setuptools@68.1.2"],
            [item["purl"] for item in query["components"]],
        )

    def test_query_inventory_excludes_unobserved_optional_component(self) -> None:
        registry, build = registry_and_build()
        optional = component("numpy", version="2.2.0", distribution="runtime-external")
        optional["purl"] = "pkg:pypi/numpy"
        optional["usages"][0]["status"] = "confirmed-optional"
        registry["components"].append(optional)

        query, index = _query_inventory(registry, build, "core-wheel")

        self.assertEqual({"setuptools"}, set(index))
        self.assertEqual(
            ["pkg:pypi/setuptools@68.1.2"],
            [item["purl"] for item in query["components"]],
        )

    def test_query_inventory_excludes_observed_optional_component(self) -> None:
        registry, build = registry_and_build()
        optional = component("numpy", version="2.2.0", distribution="runtime-external")
        optional["purl"] = "pkg:pypi/numpy"
        optional["usages"][0]["status"] = "confirmed-optional"
        registry["components"].append(optional)
        build["compliance_build"]["components"].append(
            {
                "id": "numpy",
                "version": "2.2.0",
                "usages": ["runtime-external"],
                "evidence": {"source": "python-distribution"},
            }
        )

        query, index = _query_inventory(registry, build, "core-wheel")

        self.assertEqual({"setuptools"}, set(index))
        self.assertEqual(
            ["pkg:pypi/setuptools@68.1.2"],
            [item["purl"] for item in query["components"]],
        )

    def test_query_inventory_requires_a_build_evidence_observation(self) -> None:
        registry, build = registry_and_build()
        unobserved = component("numpy", version="2.2.0", distribution="build-only")
        unobserved["purl"] = "pkg:pypi/numpy"
        unobserved["observations"] = [
            {
                "kind": "package",
                "source": "syft",
                "name": "numpy",
                "version": "2.2.0",
            }
        ]
        registry["components"].append(unobserved)

        query, index = _query_inventory(registry, build, "core-wheel")

        self.assertEqual({"setuptools"}, set(index))
        self.assertEqual(
            ["pkg:pypi/setuptools@68.1.2"],
            [item["purl"] for item in query["components"]],
        )


if __name__ == "__main__":
    unittest.main()
