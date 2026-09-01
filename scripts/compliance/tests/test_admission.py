from __future__ import annotations

import copy
import unittest
from datetime import date

from scripts.compliance.core import (
    ComplianceDataError,
    evaluate_dependency_admission,
    evaluate_inventory_audit,
)
from scripts.compliance.tests.helpers import component, scanned_coverage


TODAY = date(2026, 8, 20)


class DependencyAdmissionTests(unittest.TestCase):
    def test_admission_rejects_an_unknown_logical_target(self) -> None:
        baseline = {"schema_version": 1, "components": [component("old")]}
        current = {"schema_version": 1, "components": [component("old")]}

        with self.assertRaisesRegex(ComplianceDataError, "not declared"):
            evaluate_dependency_admission(
                baseline_registry=baseline,
                current_registry=current,
                policy=self.policy,
                declaration_delta_reports=[],
                vulnerabilities=[],
                vulnerability_coverage=[],
                today=TODAY,
                target="linux-x86_64",
            )

    def test_inventory_audit_rejects_an_unknown_logical_target(self) -> None:
        registry = {"schema_version": 1, "components": [component("demo")]}

        with self.assertRaisesRegex(ComplianceDataError, "not declared"):
            evaluate_inventory_audit(
                registry=registry,
                policy=self.policy,
                discovery_reports=[],
                vulnerabilities=[],
                vulnerability_coverage=[],
                today=TODAY,
                target="linux-x86_64",
            )

    def setUp(self) -> None:
        root = component("triton-anchor")
        root["third_party"] = False
        root["depends_on"] = ["triton"]
        triton = component("triton", distribution="distributed")
        self.baseline = {"schema_version": 1, "components": [root, triton]}
        self.current = copy.deepcopy(self.baseline)
        pybind11 = component("pybind11", distribution="build-only")
        self.current["components"].append(pybind11)
        self.current["components"][0]["depends_on"].append("pybind11")
        self.policy = {
            "schema_version": 1,
            "status": "approved",
            "expressions": {
                "MIT": {"decision": "allow"},
                "AGPL-3.0-only": {"decision": "deny"},
            },
            "vulnerability_threshold": "high",
        }

    @staticmethod
    def _registry_with_root(dependency: dict[str, object]) -> dict[str, object]:
        root = component("triton-anchor")
        root["third_party"] = False
        root["depends_on"] = [dependency["id"]]
        return {"schema_version": 1, "components": [root, dependency]}

    def _evaluate(
        self,
        declaration_name: str = "pybind11",
        vulnerabilities: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return evaluate_dependency_admission(
            baseline_registry=self.baseline,
            current_registry=self.current,
            policy=self.policy,
            declaration_delta_reports=[
                {
                    "source": "dependency-delta",
                    "status": "success",
                    "issues": [],
                    "items": [
                        {
                            "kind": "dependency-declaration",
                            "name": declaration_name,
                            "path": "pyproject.toml",
                            "candidate_usages": ["build-only"],
                        }
                    ],
                }
            ],
            vulnerabilities=vulnerabilities or [],
            vulnerability_coverage=[
                scanned_coverage("pybind11", "1.0")
            ],
            today=TODAY,
        )

    def test_reviewed_new_dependency_passes_admission(self) -> None:
        report = self._evaluate()
        self.assertEqual("pass", report["admission_status"], report)

    def test_unmapped_declaration_fails_closed(self) -> None:
        report = self._evaluate("unexpected-package")
        self.assertEqual("blocked", report["admission_status"], report)
        self.assertTrue(report["unmapped_declarations"])

    def test_denied_license_blocks_new_dependency(self) -> None:
        pybind11 = self.current["components"][-1]
        pybind11["license"]["concluded"] = "AGPL-3.0-only"
        report = self._evaluate()
        self.assertEqual("blocked", report["admission_status"], report)
        self.assertIn(
            "denied-license",
            {finding["code"] for finding in report["license_findings"]},
        )

    def test_admission_rejects_vulnerability_for_a_previous_version(self) -> None:
        report = self._evaluate(
            vulnerabilities=[
                {
                    "id": "OSV-WRONG-VERSION",
                    "component_id": "pybind11",
                    "component_version": "0.9",
                    "severity": "low",
                    "status": "open",
                }
            ]
        )

        self.assertEqual("blocked", report["admission_status"], report)
        self.assertEqual("fail", report["execution_status"], report)
        self.assertIn(
            "vulnerability-inventory-mismatch",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_reviewed_version_update_does_not_require_a_new_declaration(self) -> None:
        baseline_dependency = component("triton", version="1.0", distribution="distributed")
        current_dependency = component("triton", version="1.1", distribution="distributed")
        baseline = self._registry_with_root(baseline_dependency)
        current = self._registry_with_root(current_dependency)

        report = evaluate_dependency_admission(
            baseline_registry=baseline,
            current_registry=current,
            policy=self.policy,
            declaration_delta_reports=[
                {
                    "source": "dependency-delta",
                    "status": "success",
                    "issues": [],
                    "items": [],
                }
            ],
            vulnerabilities=[],
            vulnerability_coverage=[scanned_coverage("triton", "1.1")],
            today=TODAY,
        )

        self.assertEqual("pass", report["admission_status"], report)

    def test_full_audit_requires_version_scoped_vulnerability_coverage(self) -> None:
        dependency = component("pybind11", distribution="build-only")
        registry = self._registry_with_root(dependency)
        reports = [
            {
                "source": "build-evidence",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "build-component",
                        "component_id": "pybind11",
                        "version": "1.0",
                        "candidate_usages": ["build-only"],
                    }
                ],
            }
        ]
        blocked = evaluate_inventory_audit(
            registry=registry,
            policy=self.policy,
            discovery_reports=reports,
            vulnerabilities=[],
            vulnerability_coverage=[],
            today=TODAY,
        )
        self.assertEqual("blocked", blocked["audit_status"], blocked)

        passed = evaluate_inventory_audit(
            registry=registry,
            policy=self.policy,
            discovery_reports=reports,
            vulnerabilities=[],
            vulnerability_coverage=[
                scanned_coverage("pybind11", "1.0")
            ],
            today=TODAY,
        )
        self.assertEqual("pass", passed["audit_status"], passed)

    def test_full_audit_excludes_components_fully_classified_absent(self) -> None:
        components = []
        for component_id, category in (
            ("zstd", "runtime-external"),
            ("uv", "build-only"),
        ):
            dependency = component(component_id, distribution=category)
            dependency["version"] = {
                "value": None,
                "status": "candidate-evidence-required",
            }
            dependency["license"] = {"concluded": None, "status": "pending"}
            dependency["usages"][0]["status"] = "candidate-evidence-required"
            if component_id == "zstd":
                dependency["runtime_constraint"] = "libzstd.so.1"
            components.append(dependency)

        root = component("triton-anchor")
        root["third_party"] = False
        root["depends_on"] = [component["id"] for component in components]
        registry = {"schema_version": 1, "components": [root, *components]}
        report = evaluate_inventory_audit(
            registry=registry,
            policy=self.policy,
            discovery_reports=[
                {
                    "source": "build-evidence",
                    "status": "success",
                    "issues": [],
                    "items": [
                        {
                            "kind": "build-component",
                            "component_id": component["id"],
                            "candidate_usages": [component["usages"][0]["category"]],
                            "presence": "absent",
                            "source": "build-evidence",
                        }
                        for component in components
                    ],
                }
            ],
            vulnerabilities=[],
            vulnerability_coverage=[],
            today=TODAY,
        )

        self.assertEqual("pass", report["audit_status"], report)
        self.assertEqual([], report["inventory_findings"])
        self.assertEqual([], report["license_findings"])
        self.assertEqual([], report["vulnerability_coverage_findings"])

    def test_full_audit_rejects_vulnerability_for_an_unknown_component(self) -> None:
        dependency = component("pybind11", distribution="build-only")
        registry = self._registry_with_root(dependency)
        reports = [
            {
                "source": "build-evidence",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "build-component",
                        "component_id": "pybind11",
                        "version": "1.0",
                        "candidate_usages": ["build-only"],
                    }
                ],
            }
        ]

        report = evaluate_inventory_audit(
            registry=registry,
            policy=self.policy,
            discovery_reports=reports,
            vulnerabilities=[
                {
                    "id": "OSV-UNKNOWN-COMPONENT",
                    "component_id": "not-in-inventory",
                    "component_version": "1.0",
                    "severity": "low",
                    "status": "open",
                }
            ],
            vulnerability_coverage=[scanned_coverage("pybind11", "1.0")],
            today=TODAY,
        )

        self.assertEqual("blocked", report["audit_status"], report)
        self.assertEqual("fail", report["execution_status"], report)
        self.assertIn(
            "vulnerability-inventory-mismatch",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_full_audit_honors_release_scoped_risk_acceptance(self) -> None:
        dependency = component("pybind11", distribution="build-only")
        registry = self._registry_with_root(dependency)
        discovery_reports = [
            {
                "source": "build-evidence",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "build-component",
                        "component_id": "pybind11",
                        "version": "1.0",
                        "candidate_usages": ["build-only"],
                    }
                ],
            }
        ]
        report = evaluate_inventory_audit(
            registry=registry,
            policy=self.policy,
            discovery_reports=discovery_reports,
            vulnerabilities=[
                {
                    "id": "OSV-TEST-1",
                    "component_id": "pybind11",
                    "component_version": "1.0",
                    "severity": "high",
                    "status": "open",
                }
            ],
            vulnerability_coverage=[
                scanned_coverage("pybind11", "1.0")
            ],
            today=TODAY,
            release_version="0.2.0",
            risk_acceptances={
                "schema_version": 1,
                "records": [
                    {
                        "id": "RA-TEST-1",
                        "vulnerability_id": "OSV-TEST-1",
                        "component_id": "pybind11",
                        "component_version": "1.0",
                        "release_version": "0.2.0",
                        "status": "accepted",
                        "reason": "Exposure is isolated until the scheduled upgrade.",
                        "approved_by": "release-leader",
                        "approved_on": "2026-08-01",
                        "expires_on": "2026-09-01",
                    }
                ],
            },
        )
        self.assertEqual("pass", report["audit_status"], report)


if __name__ == "__main__":
    unittest.main()
