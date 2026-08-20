from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.compliance.core import (
    artifact_links,
    evaluate_artifact,
    evaluate_candidate,
    generate_sbom,
    write_json,
)

from scripts.compliance.tests.helpers import artifact, component, scanned_coverage


TODAY = date(2026, 8, 19)


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.components = [component("triton", licenses=["MIT"])]
        self.artifacts = [
            artifact("cp311-x86_64", "1" * 64, "cp311-cp311-linux_x86_64"),
            artifact("cp312-x86_64", "2" * 64, "cp312-cp312-linux_x86_64"),
        ]

    def test_every_candidate_maps_to_its_own_matching_sbom(self) -> None:
        sboms = {
            item["id"]: generate_sbom(item, self.components) for item in self.artifacts
        }
        links = artifact_links(self.artifacts, sboms)
        self.assertEqual(
            {"cp311-x86_64", "cp312-x86_64"},
            {entry["artifact_id"] for entry in links["artifacts"]},
        )
        for item in self.artifacts:
            hashes = sboms[item["id"]]["metadata"]["component"]["hashes"]
            self.assertIn(
                {"alg": "SHA-256", "content": item["sha256"]}, hashes
            )

    def test_missing_or_mismatched_candidate_sbom_is_rejected(self) -> None:
        sboms = {
            item["id"]: generate_sbom(item, self.components) for item in self.artifacts
        }
        missing = dict(sboms)
        missing.pop("cp312-x86_64")
        with self.assertRaises(ValueError):
            artifact_links(self.artifacts, missing)

        mismatched = copy.deepcopy(sboms)
        mismatched["cp312-x86_64"]["metadata"]["component"]["hashes"] = [
            {"alg": "SHA-256", "content": "f" * 64}
        ]
        with self.assertRaises(ValueError):
            artifact_links(self.artifacts, mismatched)

        wrong_version = copy.deepcopy(sboms)
        wrong_version["cp312-x86_64"]["metadata"]["component"]["version"] = "9.9.9"
        with self.assertRaises(ValueError):
            artifact_links(self.artifacts, wrong_version)

    def test_declared_sbom_hash_matches_written_file_bytes(self) -> None:
        candidate = self.artifacts[0]
        sbom = generate_sbom(candidate, self.components)
        link = artifact_links([candidate], {candidate["id"]: sbom})["artifacts"][0]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "candidate.cdx.json"
            write_json(output, sbom)
            self.assertEqual(
                link["sbom"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest()
            )

    def test_build_formulation_does_not_duplicate_product_components(self) -> None:
        dual_use = component("dual-use")
        dual_use["usages"].append(
            {
                "category": "build-only",
                "target": "core-wheel",
                "declaration_patterns": ["pyproject.toml"],
            }
        )
        build_only = component("builder", distribution="build-only")
        sbom = generate_sbom(self.artifacts[0], [dual_use, build_only])
        product_refs = {item["bom-ref"] for item in sbom["components"]}
        build_refs = {
            item["bom-ref"] for item in sbom["formulation"][0]["components"]
        }
        self.assertFalse(product_refs & build_refs)

    def test_dependency_graph_distinguishes_direct_and_transitive_components(self) -> None:
        root = component("triton-anchor")
        root["third_party"] = False
        root["depends_on"] = ["triton"]
        direct = component("triton", distribution="distributed")
        direct["depends_on"] = ["f2reduce"]
        transitive = component("f2reduce", distribution="embedded")
        sbom = generate_sbom(self.artifacts[0], [root, direct, transitive])
        refs = {
            item["name"]: item["bom-ref"] for item in sbom["components"]
        }
        dependencies = {item["ref"]: item["dependsOn"] for item in sbom["dependencies"]}
        root_ref = sbom["metadata"]["component"]["bom-ref"]
        self.assertEqual([refs["triton"]], dependencies[root_ref])
        self.assertEqual([refs["f2reduce"]], dependencies[refs["triton"]])

    def test_optional_runtime_dependency_is_marked_optional(self) -> None:
        optional = component("numpy", distribution="runtime-external")
        optional["usages"][0]["status"] = "confirmed-optional"
        sbom = generate_sbom(self.artifacts[0], [optional])
        self.assertEqual("optional", sbom["components"][0]["scope"])

    def test_observed_pypi_version_is_added_to_the_sbom_purl(self) -> None:
        setuptools = component("setuptools", distribution="runtime-external")
        setuptools["purl"] = "pkg:pypi/setuptools"
        setuptools["version"] = {
            "value": ">=64",
            "kind": "version-constraint",
            "status": "constraint-only",
        }
        setuptools["observations"] = [
            {"version": "68.1.2", "source": "build-evidence"}
        ]

        sbom = generate_sbom(self.artifacts[0], [setuptools])

        self.assertEqual(
            "pkg:pypi/setuptools@68.1.2", sbom["components"][0]["purl"]
        )

    def test_constraint_only_version_is_not_added_to_the_sbom_purl(self) -> None:
        setuptools = component("setuptools", distribution="runtime-external")
        setuptools["purl"] = "pkg:pypi/setuptools"
        setuptools["version"] = {
            "value": ">=64",
            "kind": "version-constraint",
            "status": "constraint-only",
        }

        sbom = generate_sbom(self.artifacts[0], [setuptools])

        self.assertEqual("pkg:pypi/setuptools", sbom["components"][0]["purl"])

    def _candidate_gate_inputs(self) -> dict[str, object]:
        candidate = self.artifacts[0]
        root = component("triton-anchor", licenses=["Apache-2.0"])
        root["third_party"] = False
        root["depends_on"] = ["triton"]
        root["usages"][0]["artifact_patterns"] = ["triton_anchor/**"]
        dependency = component("triton", distribution="distributed", licenses=["MIT"])
        dependency["usages"][0]["artifact_patterns"] = ["triton/**"]
        builder = component("builder", distribution="build-only", licenses=["MIT"])
        registry = {
            "schema_version": 1,
            "components": [root, dependency, builder],
        }
        discovery_reports = [
            {
                "source": "wheel",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "artifact-file",
                        "path": "triton/__init__.py",
                        "source": "wheel",
                    }
                ],
            },
            {
                "source": "scancode-source",
                "status": "success",
                "issues": [],
                "items": [],
            },
            {
                "source": "scancode-wheel",
                "status": "success",
                "issues": [],
                "items": [],
            },
            {
                "source": "syft",
                "status": "success",
                "issues": [],
                "items": [{"kind": "package", "name": "triton", "version": "1.0"}],
            },
            {
                "source": "build-evidence",
                "status": "success",
                "artifact_sha256": candidate["sha256"],
                "evidence_binding": "same-build",
                "issues": [],
                "items": [
                    {
                        "kind": "build-component",
                        "component_id": "builder",
                        "version": "1.0",
                        "candidate_usages": ["build-only"],
                        "source": "build-evidence",
                    }
                ],
            },
            {"source": "osv", "status": "success", "issues": [], "items": []},
        ]
        return {
            "artifact": candidate,
            "registry": registry,
            "policy": {
                "schema_version": 1,
                "status": "approved",
                "expressions": {
                    "Apache-2.0": {"decision": "allow"},
                    "MIT": {"decision": "allow"},
                    "AGPL-3.0-only": {"decision": "deny"},
                },
                "vulnerability_threshold": "high",
            },
            "risk_acceptances": {"schema_version": 1, "records": []},
            "discovery_reports": discovery_reports,
            "vulnerabilities": [],
            "vulnerability_coverage": [
                scanned_coverage("triton", "1.0"),
                scanned_coverage("builder", "1.0"),
            ],
            "today": TODAY,
        }

    def test_single_candidate_gate_blocks_denied_license(self) -> None:
        inputs = self._candidate_gate_inputs()
        report, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("pass", report["promotion_status"], report)

        blocked = self._candidate_gate_inputs()
        blocked["registry"]["components"][1]["license"]["concluded"] = (
            "AGPL-3.0-only"
        )
        report, _, _, _ = evaluate_candidate(**blocked)
        self.assertEqual("blocked", report["promotion_status"], report)

    def test_explicitly_absent_conditional_component_is_not_in_the_sbom(self) -> None:
        inputs = self._candidate_gate_inputs()
        conditional = component("zstd", distribution="runtime-external")
        conditional["usages"][0]["status"] = "candidate-evidence-required"
        inputs["registry"]["components"].append(conditional)

        unclassified, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("blocked", unclassified["promotion_status"])

        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        build["items"].append(
            {
                "kind": "build-component",
                "component_id": "zstd",
                "candidate_usages": ["runtime-external"],
                "presence": "absent",
                "source": "build-evidence",
            }
        )
        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("pass", report["promotion_status"], report)
        self.assertNotIn("zstd", {item["name"] for item in sbom["components"]})

    def test_technical_evaluation_and_candidate_share_the_same_sbom(self) -> None:
        inputs = self._candidate_gate_inputs()

        technical_report, technical_sbom, technical_link, technical_notices = (
            evaluate_artifact(**inputs)
        )
        candidate_report, candidate_sbom, candidate_link, candidate_notices = (
            evaluate_candidate(**inputs)
        )

        self.assertEqual("not-applicable", technical_report["promotion_status"])
        self.assertEqual("pass", candidate_report["promotion_status"])
        self.assertEqual(technical_sbom, candidate_sbom)
        self.assertEqual(technical_link, candidate_link)
        self.assertEqual(technical_notices, candidate_notices)
        self.assertEqual(
            technical_report["compliance_blockers"],
            candidate_report["compliance_blockers"],
        )

    def test_formal_candidate_requires_same_build_binding(self) -> None:
        inputs = self._candidate_gate_inputs()
        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        build["evidence_binding"] = "post-hoc"

        technical_report, _, _, _ = evaluate_artifact(**inputs)
        candidate_report, _, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("pass", technical_report["execution_status"])
        self.assertEqual("pass", technical_report["compliance_status"])
        self.assertEqual("not-applicable", technical_report["promotion_status"])
        self.assertEqual("pass", candidate_report["execution_status"])
        self.assertEqual("pass", candidate_report["compliance_status"])
        self.assertEqual("blocked", candidate_report["promotion_status"])
        self.assertEqual(
            {"candidate-build-binding-not-established"},
            {
                finding["code"]
                for finding in candidate_report["promotion_findings"]
            },
        )

    def test_core_rechecks_normalized_build_artifact_binding(self) -> None:
        inputs = self._candidate_gate_inputs()
        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        build["artifact_sha256"] = "f" * 64

        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["execution_status"])
        self.assertEqual("incomplete", report["evidence_status"])
        self.assertEqual("incomplete", report["sbom_inventory_status"])
        self.assertEqual("incomplete", sbom["compositions"][0]["aggregate"])
        self.assertEqual("blocked", report["promotion_status"])
        self.assertIn(
            "artifact-binding-mismatch",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_core_rejects_invalid_normalized_build_binding(self) -> None:
        inputs = self._candidate_gate_inputs()
        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        build["evidence_binding"] = "trusted-by-claim"

        report, _, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["execution_status"])
        self.assertEqual("incomplete", report["evidence_status"])
        self.assertIn(
            "invalid-evidence-binding",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_candidate_rejects_empty_normalized_build_inventory(self) -> None:
        inputs = self._candidate_gate_inputs()
        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        build["items"] = []

        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["execution_status"])
        self.assertEqual("incomplete", report["evidence_status"])
        self.assertEqual("incomplete", report["sbom_inventory_status"])
        self.assertEqual("incomplete", sbom["compositions"][0]["aggregate"])
        self.assertIn(
            "empty-build-inventory",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_candidate_rejects_duplicate_evidence_source(self) -> None:
        inputs = self._candidate_gate_inputs()
        build = next(
            report
            for report in inputs["discovery_reports"]
            if report["source"] == "build-evidence"
        )
        inputs["discovery_reports"].append(copy.deepcopy(build))

        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["execution_status"])
        self.assertEqual("incomplete", report["evidence_status"])
        self.assertEqual("incomplete", report["sbom_inventory_status"])
        self.assertEqual("incomplete", sbom["compositions"][0]["aggregate"])
        self.assertEqual("blocked", report["promotion_status"])
        self.assertIn(
            "duplicate-evidence-source",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_denied_license_does_not_make_complete_sbom_incomplete(self) -> None:
        inputs = self._candidate_gate_inputs()
        inputs["registry"]["components"][1]["license"]["concluded"] = (
            "AGPL-3.0-only"
        )

        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["compliance_status"])
        self.assertEqual("complete", report["sbom_inventory_status"])
        self.assertEqual("complete", sbom["compositions"][0]["aggregate"])

    def test_high_vulnerability_does_not_make_complete_sbom_incomplete(self) -> None:
        inputs = self._candidate_gate_inputs()
        inputs["vulnerabilities"] = [
            {
                "id": "OSV-HIGH",
                "component_id": "triton",
                "component_version": "1.0",
                "severity": "high",
                "status": "open",
            }
        ]

        report, sbom, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("fail", report["compliance_status"])
        self.assertEqual("complete", report["sbom_inventory_status"])
        self.assertEqual("complete", sbom["compositions"][0]["aggregate"])

    def test_single_candidate_gate_blocks_incomplete_scan(self) -> None:
        inputs = self._candidate_gate_inputs()
        inputs["discovery_reports"][1] = {
            "source": "scancode-source",
            "status": "failed",
            "issues": ["scan failed"],
            "items": [],
        }
        report, _, _, _ = evaluate_candidate(**inputs)
        self.assertEqual("blocked", report["promotion_status"], report)

    def test_candidate_rejects_vulnerability_for_a_different_version(self) -> None:
        inputs = self._candidate_gate_inputs()
        inputs["vulnerabilities"] = [
            {
                "id": "OSV-WRONG-VERSION",
                "component_id": "triton",
                "component_version": "0.9",
                "severity": "low",
                "status": "open",
            }
        ]

        report, _, _, _ = evaluate_candidate(**inputs)

        self.assertEqual("blocked", report["promotion_status"], report)
        self.assertEqual("fail", report["execution_status"], report)
        self.assertIn(
            "vulnerability-inventory-mismatch",
            {issue.get("code") for issue in report["execution_issues"]},
        )

    def test_candidate_ignores_explicit_test_and_ci_only_findings(self) -> None:
        for category in ("test-only", "CI-only"):
            with self.subTest(category=category):
                inputs = self._candidate_gate_inputs()
                out_of_scope = component("test-helper", distribution=category)
                inputs["registry"]["components"].append(out_of_scope)
                inputs["vulnerabilities"] = [
                    {
                        "id": "OSV-OUT-OF-SCOPE",
                        "component_id": "test-helper",
                        "component_version": "1.0",
                        "severity": "critical",
                        "status": "open",
                    }
                ]

                report, _, _, _ = evaluate_candidate(**inputs)

                self.assertEqual("pass", report["promotion_status"], report)


if __name__ == "__main__":
    unittest.main()
