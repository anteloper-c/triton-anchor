from __future__ import annotations

import copy
import unittest
from datetime import date

from scripts.compliance.core import (
    evaluate_vulnerabilities,
    evaluate_vulnerability_coverage,
)

from scripts.compliance.tests.helpers import component, scanned_coverage


ARTIFACT_SHA256 = "a" * 64
TODAY = date(2026, 8, 19)
VULNERABILITY = {
    "id": "OSV-TEST-1",
    "component_id": "triton",
    "component_version": "abc123",
    "severity": "HIGH",
    "status": "open",
}
ACCEPTANCE = {
    "id": "RA-TEST-1",
    "vulnerability_id": "OSV-TEST-1",
    "component_id": "triton",
    "component_version": "abc123",
    "artifact_sha256": ARTIFACT_SHA256,
    "status": "accepted",
    "reason": "Upgrade is scheduled; exposure is isolated until then.",
    "approved_by": "release-leader",
    "approved_on": "2026-08-01",
    "expires_on": "2026-09-01",
}


class VulnerabilityPolicyTests(unittest.TestCase):
    def test_severe_vulnerability_without_acceptance_blocks(self) -> None:
        findings = evaluate_vulnerabilities(
            [VULNERABILITY], [], ARTIFACT_SHA256, TODAY
        )
        self.assertIn(
            "unresolved-severe-vulnerability", {finding["code"] for finding in findings}
        )

    def test_complete_matching_unexpired_acceptance_allows(self) -> None:
        self.assertEqual(
            [],
            evaluate_vulnerabilities(
                [VULNERABILITY], [ACCEPTANCE], ARTIFACT_SHA256, TODAY
            ),
        )

    def test_missing_required_acceptance_fields_never_allows(self) -> None:
        required = (
            "id",
            "vulnerability_id",
            "component_id",
            "component_version",
            "artifact_sha256",
            "status",
            "reason",
            "approved_by",
            "approved_on",
            "expires_on",
        )
        for field in required:
            with self.subTest(field=field):
                acceptance = copy.deepcopy(ACCEPTANCE)
                acceptance.pop(field)
                findings = evaluate_vulnerabilities(
                    [VULNERABILITY], [acceptance], ARTIFACT_SHA256, TODAY
                )
                self.assertTrue(findings)

    def test_expired_or_wrongly_scoped_acceptance_does_not_match(self) -> None:
        variants = []
        for field, value in (
            ("expires_on", "2026-08-18"),
            ("vulnerability_id", "OSV-OTHER"),
            ("component_id", "llvm"),
            ("component_version", "different"),
            ("artifact_sha256", "b" * 64),
        ):
            acceptance = copy.deepcopy(ACCEPTANCE)
            acceptance[field] = value
            variants.append((field, acceptance))
        for field, acceptance in variants:
            with self.subTest(field=field):
                findings = evaluate_vulnerabilities(
                    [VULNERABILITY], [acceptance], ARTIFACT_SHA256, TODAY
                )
                self.assertTrue(findings)

    def test_fixed_severe_vulnerability_does_not_need_acceptance(self) -> None:
        fixed = dict(
            VULNERABILITY,
            status="fixed",
            resolution_evidence="https://example.test/fix/OSV-TEST-1",
        )
        self.assertEqual(
            [], evaluate_vulnerabilities([fixed], [], ARTIFACT_SHA256, TODAY)
        )

    def test_claimed_disposition_without_evidence_still_blocks(self) -> None:
        fixed = dict(VULNERABILITY, status="fixed")
        self.assertTrue(
            evaluate_vulnerabilities([fixed], [], ARTIFACT_SHA256, TODAY)
        )

    def test_release_scoped_acceptance_covers_same_version_wheels(self) -> None:
        acceptance = copy.deepcopy(ACCEPTANCE)
        acceptance.pop("artifact_sha256")
        acceptance["release_version"] = "0.2.0"
        self.assertEqual(
            [],
            evaluate_vulnerabilities(
                [VULNERABILITY],
                [acceptance],
                "b" * 64,
                TODAY,
                artifact_version="0.2.0",
            ),
        )

    def test_zero_findings_without_component_coverage_does_not_pass(self) -> None:
        dependency = component("triton", distribution="distributed")
        dependency["active_usages"] = ["distributed"]
        findings = evaluate_vulnerability_coverage(
            [dependency], [], "core-wheel"
        )
        self.assertEqual("vulnerability-coverage-gap", findings[0]["code"])

        self.assertEqual(
            [],
            evaluate_vulnerability_coverage(
                [dependency],
                [scanned_coverage("triton", "1.0")],
                "core-wheel",
            ),
        )

    def test_scanned_coverage_requires_tool_and_result_evidence(self) -> None:
        dependency = component("triton", distribution="distributed")
        dependency["active_usages"] = ["distributed"]

        findings = evaluate_vulnerability_coverage(
            [dependency],
            [
                {
                    "component_id": "triton",
                    "component_version": "1.0",
                    "status": "scanned",
                }
            ],
            "core-wheel",
        )
        self.assertTrue(findings)
        self.assertIn("missing tool or result evidence", findings[0]["reason"])

    def test_unresolved_version_constraint_does_not_create_duplicate_gap(self) -> None:
        dependency = component("cmake", distribution="build-only")
        dependency["version"] = {
            "value": ">=3.18",
            "kind": "semver-constraint",
            "status": "constraint-only",
        }
        dependency["active_usages"] = ["build-only"]

        self.assertEqual(
            [], evaluate_vulnerability_coverage([dependency], [], "core-wheel")
        )

    def test_runtime_constraint_requires_reviewed_coverage(self) -> None:
        dependency = component("zstd", distribution="runtime-external")
        dependency["version"] = {"value": None, "status": "candidate-evidence-required"}
        dependency["runtime_constraint"] = "libzstd.so.1"
        dependency["active_usages"] = ["runtime-external"]

        missing = evaluate_vulnerability_coverage(
            [dependency], [], "core-wheel"
        )
        self.assertEqual("libzstd.so.1", missing[0]["component_version"])

        automatic_without_a_version = evaluate_vulnerability_coverage(
            [dependency],
            [
                {
                    "component_id": "zstd",
                    "component_version": "libzstd.so.1",
                    "status": "scanned",
                }
            ],
            "core-wheel",
        )
        self.assertTrue(automatic_without_a_version)

        reviewed = evaluate_vulnerability_coverage(
            [dependency],
            [
                {
                    "component_id": "zstd",
                    "component_version": "libzstd.so.1",
                    "status": "reviewed",
                    "evidence": "https://example.test/reviews/zstd-soname-1",
                    "reviewed_by": "security-reviewer",
                    "reviewed_on": "2026-08-20",
                }
            ],
            "core-wheel",
        )
        self.assertEqual([], reviewed)


if __name__ == "__main__":
    unittest.main()
