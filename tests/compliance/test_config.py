from __future__ import annotations

import copy
import unittest
from pathlib import Path

from scripts.compliance.core import (
    ComplianceDataError,
    load_json,
    notice_entries,
    render_notices,
    validate_policy,
    validate_registry,
    validate_risk_acceptances,
    validate_target,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class CheckedInConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_json(REPOSITORY_ROOT / "compliance/component-registry.json")
        self.policy = load_json(REPOSITORY_ROOT / "compliance/license-policy.json")
        self.risk_acceptances = load_json(
            REPOSITORY_ROOT / "compliance/risk-acceptances.json"
        )

    def test_checked_in_configuration_matches_the_gate_contract(self) -> None:
        validate_registry(self.registry)
        validate_policy(self.policy)
        validate_risk_acceptances(self.risk_acceptances)

    def test_unknown_schema_versions_fail_closed(self) -> None:
        for validator, document in (
            (validate_registry, self.registry),
            (validate_policy, self.policy),
            (validate_risk_acceptances, self.risk_acceptances),
        ):
            with self.subTest(validator=validator.__name__):
                changed = copy.deepcopy(document)
                changed["schema_version"] = 2
                with self.assertRaises(ComplianceDataError):
                    validator(changed)

    def test_dangling_component_evidence_reference_is_rejected(self) -> None:
        changed = copy.deepcopy(self.registry)
        changed["components"][0]["usages"][0]["evidence_ids"].append("missing")
        with self.assertRaises(ComplianceDataError):
            validate_registry(changed)

    def test_ci_only_usage_cannot_declare_an_artifact_target(self) -> None:
        changed = copy.deepcopy(self.registry)
        changed["components"][0]["usages"].append(
            {
                "category": "CI-only",
                "status": "confirmed",
                "target": "ci-only-target",
                "path_patterns": [".github/**"],
                "evidence_ids": [],
            }
        )
        validate_registry(changed)
        with self.assertRaisesRegex(ComplianceDataError, "first-party product"):
            validate_target(changed, "ci-only-target")

    def test_policy_has_one_approval_state(self) -> None:
        self.assertFalse(
            any(
                "approval_status" in entry
                for entry in self.policy["expressions"].values()
            )
        )

    def test_confirmed_python_package_identities_have_base_purls(self) -> None:
        expected = {
            "pybind11": "pkg:pypi/pybind11",
            "setuptools": "pkg:pypi/setuptools",
            "wheel-build-package": "pkg:pypi/wheel",
            "pypa-build": "pkg:pypi/build",
            "numpy": "pkg:pypi/numpy",
            "matplotlib": "pkg:pypi/matplotlib",
            "pandas": "pkg:pypi/pandas",
            "psutil": "pkg:pypi/psutil",
            "pytorch": "pkg:pypi/torch",
            "redis-py": "pkg:pypi/redis",
        }
        by_id = {
            component["id"]: component for component in self.registry["components"]
        }

        self.assertEqual(
            expected,
            {
                component_id: by_id[component_id].get("purl")
                for component_id in expected
            },
        )

    def test_canonical_notice_matches_the_component_registry(self) -> None:
        expected = render_notices(notice_entries(self.registry, "core-wheel"))
        committed = (REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(expected, committed.replace("\r\n", "\n"))

    def test_accepted_risk_record_needs_an_id_and_explicit_scope(self) -> None:
        record = {
            "status": "accepted",
            "component_id": "triton",
            "component_version": "1",
            "vulnerability_id": "OSV-1",
            "reason": "temporary isolation",
            "approved_by": "leader",
            "approved_on": "2026-08-01",
            "expires_on": "2026-09-01",
        }
        changed = {"schema_version": 1, "records": [record]}
        with self.assertRaises(ComplianceDataError):
            validate_risk_acceptances(changed)

        record["id"] = "RA-1"
        with self.assertRaises(ComplianceDataError):
            validate_risk_acceptances(changed)

        record["release_version"] = "0.2.0"
        validate_risk_acceptances(changed)


if __name__ == "__main__":
    unittest.main()
