from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.compliance.osv_runner import _query_inventory, run_osv_scan
from tests.compliance.helpers import component


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

    def test_empty_ecosystem_is_not_automatic_coverage(self) -> None:
        code, _, output, _ = self._run(0, raw_report(ecosystem=""))

        self.assertEqual(0, code)
        self.assertEqual([], output["coverage"])
        self.assertNotIn(
            "purl", output["results"][0]["packages"][0]["package"]
        )

    def test_wrong_ecosystem_is_not_automatic_coverage(self) -> None:
        code, _, output, _ = self._run(0, raw_report(ecosystem="npm"))

        self.assertEqual(0, code)
        self.assertEqual([], output["coverage"])
        self.assertNotIn(
            "purl", output["results"][0]["packages"][0]["package"]
        )

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
