from __future__ import annotations

import unittest

from scripts.compliance.core import (
    _usage_categories,
    diff_components,
    evaluate_licenses,
    normalize_build_evidence,
    normalize_cyclonedx,
    normalize_scancode,
    notice_entries,
    reconcile_discoveries,
    validate_notice_coverage,
)
from scripts.compliance.model import _effective_version

from scripts.compliance.tests.helpers import component


class ComponentAndLicenseTests(unittest.TestCase):
    def test_constraint_uses_compatible_more_precise_observation(self) -> None:
        dependency = component("cpython")
        dependency["version"] = {
            "value": ">=3.8",
            "kind": "semver-constraint",
            "status": "constraint-only",
        }
        dependency["observations"] = [
            {"version": "3.12", "source": "wheel-tag"},
            {"version": "3.12.3", "source": "build-evidence"},
        ]
        self.assertEqual(("3.12.3", "observed"), _effective_version(dependency))

        dependency["observations"].append(
            {"version": "3.11.9", "source": "conflicting-evidence"}
        )
        self.assertEqual((None, "conflict"), _effective_version(dependency))

    def test_active_usage_does_not_leak_between_targets(self) -> None:
        dependency = component("flaggems", distribution="test-only")
        dependency["usages"][0]["target"] = "compatibility-tests"
        dependency["active_usages"] = ["test-only"]

        self.assertEqual(
            set(), _usage_categories(dependency, "core-wheel", active=True)
        )
        self.assertEqual(
            {"test-only"},
            _usage_categories(dependency, "compatibility-tests", active=True),
        )
        discovery = {
            "source": "fixture",
            "status": "success",
            "issues": [],
            "items": [
                {"kind": "source-snapshot-file", "path": "flaggems/module.py"}
            ],
        }
        core_result = reconcile_discoveries(
            {"schema_version": 1, "components": [dependency]},
            [discovery],
            target="core-wheel",
        )
        compatibility_result = reconcile_discoveries(
            {"schema_version": 1, "components": [dependency]},
            [discovery],
            target="compatibility-tests",
        )
        self.assertEqual(1, len(core_result["unmapped"]))
        self.assertEqual(1, len(compatibility_result["mapped"]))

    def test_raw_scancode_paths_are_relative_to_the_selected_input(self) -> None:
        report = {
            "headers": [
                {
                    "options": {"input": ["/workspace/wheel-unpacked"]},
                    "errors": [],
                }
            ],
            "files": [
                {"path": "wheel-unpacked", "type": "directory"},
                {
                    "path": "wheel-unpacked/triton/LICENSE",
                    "type": "file",
                    "scan_errors": [],
                    "license_detections": [
                        {"license_expression_spdx": "MIT"}
                    ],
                },
            ],
            "packages": [],
        }
        normalized = normalize_scancode(
            report, "scancode-wheel", "artifact"
        )

        self.assertEqual("success", normalized["status"], normalized)
        self.assertEqual("triton/LICENSE", normalized["items"][0]["path"])
        self.assertEqual("artifact", normalized["items"][0]["path_scope"])

    def test_empty_scanner_inventories_are_not_successful_evidence(self) -> None:
        scancode = normalize_scancode(
            {
                "headers": [
                    {"options": {"input": ["/workspace/source"]}, "errors": []}
                ],
                "files": [{"path": "source", "type": "directory"}],
                "packages": [],
            }
        )
        syft = normalize_cyclonedx(
            {
                "bomFormat": "CycloneDX",
                "components": [{"type": "file", "name": "module.py"}],
            }
        )

        self.assertEqual("failed", scancode["status"])
        self.assertEqual("failed", syft["status"])

    def test_syft_github_actions_are_retained_as_ci_only_exclusions(self) -> None:
        syft = normalize_cyclonedx(
            {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "type": "library",
                        "name": "actions/checkout",
                        "version": "v4",
                        "purl": "pkg:github/actions/checkout@v4",
                    }
                ],
            }
        )

        self.assertEqual("success", syft["status"], syft)
        self.assertTrue(syft["items"][0]["excluded"])
        self.assertIn("CI-only", syft["items"][0]["reason"])

    def test_artifact_license_uses_artifact_ownership_patterns(self) -> None:
        dependency = component("triton", owned_paths=["source/triton/"])
        dependency["usages"][0]["artifact_patterns"] = ["triton/**"]
        normalized = normalize_scancode(
            {
                "headers": [
                    {
                        "options": {"input": ["/workspace/wheel-unpacked"]},
                        "errors": [],
                    }
                ],
                "files": [
                    {"path": "wheel-unpacked", "type": "directory"},
                    {
                        "path": "wheel-unpacked/triton/LICENSE",
                        "type": "file",
                        "scan_errors": [],
                        "license_detections": [
                            {"license_expression_spdx": "MIT"}
                        ],
                    },
                ],
                "packages": [],
            },
            "scancode-wheel",
            "artifact",
        )
        reconciled = reconcile_discoveries([dependency], [normalized])

        self.assertEqual([], reconciled["unmapped"], reconciled)
        self.assertEqual(
            ["triton"], reconciled["mapped"][0]["component_ids"]
        )

    def test_versioned_discovery_purl_matches_base_registry_purl(self) -> None:
        dependency = component("canonical-name")
        dependency["purl"] = "pkg:pypi/setuptools"
        discoveries = [
            {
                "source": "syft",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "package",
                        "name": "scanner-name-does-not-match",
                        "purl": "pkg:pypi/setuptools@68.1.2",
                        "version": "68.1.2",
                        "source": "syft",
                    }
                ],
            }
        ]

        reconciled = reconcile_discoveries([dependency], discoveries)

        self.assertEqual([], reconciled["unmapped"], reconciled)
        self.assertEqual(
            ["canonical-name"], reconciled["mapped"][0]["component_ids"]
        )

    def test_real_scancode_path_finding_reaches_license_gate(self) -> None:
        registry = [component("triton", owned_paths=["triton/"])]
        discoveries = normalize_scancode(
            {
                "headers": [
                    {
                        "options": {"input": ["/workspace/source"]},
                        "errors": [],
                    }
                ],
                "files": [
                    {"path": "source", "type": "directory"},
                    {
                        "path": "source/triton/LICENSE",
                        "type": "file",
                        "scan_errors": [],
                        "license_detections": [
                            {"license_expression_spdx": "AGPL-3.0-only"}
                        ],
                    },
                ],
                "packages": [],
            },
        )
        reconciled = reconcile_discoveries(registry, [discoveries])
        triton = next(item for item in reconciled["components"] if item["id"] == "triton")
        self.assertIn(
            "AGPL-3.0-only",
            {item.get("license_expression") for item in triton["observations"]},
        )

        findings = evaluate_licenses(
            reconciled["components"],
            {"expression_decisions": {"MIT": "allow", "AGPL-3.0-only": "deny"}},
        )
        self.assertIn("denied-license", {finding["code"] for finding in findings})

    def test_full_spdx_expressions_use_explicit_policy_decisions(self) -> None:
        policy = {
            "expression_decisions": {
                "MIT": "allow",
                "Apache-2.0 WITH LLVM-exception": "allow",
                "MIT AND AGPL-3.0-only": "deny",
            }
        }
        components = [
            component("llvm", licenses=["Apache-2.0 WITH LLVM-exception"]),
            component("bad", licenses=["MIT AND AGPL-3.0-only"]),
            component("review", licenses=["MIT OR AGPL-3.0-only"]),
        ]
        findings = evaluate_licenses(components, policy)
        by_component = {finding["component_id"]: finding["code"] for finding in findings}
        self.assertNotIn("llvm", by_component)
        self.assertEqual("denied-license", by_component["bad"])
        self.assertEqual("license-review-required", by_component["review"])

    def test_scanner_failure_is_an_execution_issue(self) -> None:
        failed_scan = normalize_scancode(
            {
                "headers": [
                    {
                        "options": {"input": ["/workspace/source"]},
                        "errors": ["report-missing"],
                    }
                ],
                "files": [
                    {"path": "source", "type": "directory"},
                    {
                        "path": "source/module.py",
                        "type": "file",
                        "scan_errors": [],
                        "license_detections": [],
                    },
                ],
                "packages": [],
            }
        )
        reconciled = reconcile_discoveries(
            [component("triton")],
            [failed_scan],
        )
        self.assertIn("scanner-failed", {issue["code"] for issue in reconciled["issues"]})

    def test_new_native_file_requires_component_ownership(self) -> None:
        discoveries = [
            {
                "source": "wheel",
                "status": "success",
                "issues": [],
                "items": [
                    {
                        "kind": "artifact-file",
                        "path": "plugins/new.so",
                        "path_scope": "artifact",
                        "sha256": "a" * 64,
                        "allow_multiple": True,
                    }
                ],
            }
        ]
        uncovered = reconcile_discoveries([component("triton")], discoveries)
        self.assertIn(
            "unmapped-distributed-file", {issue["code"] for issue in uncovered["issues"]}
        )

        covered = reconcile_discoveries(
            [component("plugin", owned_paths=["plugins/"], licenses=["MIT"])],
            discoveries,
        )
        self.assertNotIn(
            "unmapped-distributed-file", {issue["code"] for issue in covered["issues"]}
        )

    def test_shared_native_container_does_not_prove_embedded_component(self) -> None:
        root = component("triton-anchor")
        root["third_party"] = False
        root["usages"][0]["category"] = "distributed"
        root["usages"][0]["artifact_patterns"] = ["triton/_C/libtriton.so"]
        embedded = component("llvm", distribution="embedded")
        embedded["usages"][0].pop("artifact_patterns")
        embedded["usages"][0]["container_artifact_patterns"] = [
            "triton/_C/libtriton.so"
        ]
        wheel = {
            "source": "wheel",
            "status": "success",
            "issues": [],
            "items": [
                {
                    "kind": "artifact-file",
                    "path": "triton/_C/libtriton.so",
                    "source": "wheel",
                }
            ],
        }
        reconciled = reconcile_discoveries([root, embedded], [wheel])
        llvm = next(
            item for item in reconciled["components"] if item["id"] == "llvm"
        )
        self.assertNotIn("embedded", llvm["active_usages"])

        build = normalize_build_evidence(
            {
                "status": "success",
                "artifact_sha256": "a" * 64,
                "evidence_binding": "same-build",
                "components": [
                    {"id": "llvm", "version": "1.0", "usages": ["embedded"]}
                ],
            },
            "a" * 64,
        )
        self.assertEqual("a" * 64, build["artifact_sha256"])
        self.assertEqual("same-build", build["evidence_binding"])
        reconciled = reconcile_discoveries([root, embedded], [wheel, build])
        llvm = next(
            item for item in reconciled["components"] if item["id"] == "llvm"
        )
        self.assertIn("embedded", llvm["active_usages"])

    def test_build_evidence_rejects_unknown_binding_value(self) -> None:
        build = normalize_build_evidence(
            {
                "status": "success",
                "artifact_sha256": "a" * 64,
                "evidence_binding": "claimed-trusted",
                "components": [],
            },
            "a" * 64,
        )

        self.assertEqual("failed", build["status"])
        self.assertIn("build evidence binding", build["issues"][0])

    def test_build_evidence_requires_explicit_status_and_component_array(
        self,
    ) -> None:
        cases = (
            {
                "artifact_sha256": "a" * 64,
                "evidence_binding": "same-build",
                "components": [
                    {"id": "builder", "version": "1.0", "usages": ["build-only"]}
                ],
            },
            {
                "status": "success",
                "artifact_sha256": "a" * 64,
                "evidence_binding": "same-build",
                "components": {"id": "builder"},
            },
            {
                "status": "success",
                "artifact_sha256": "a" * 64,
                "evidence_binding": "same-build",
                "components": [],
            },
            {
                "status": "success",
                "artifact_sha256": "a" * 64,
                "evidence_binding": "same-build",
                "components": [
                    {
                        "id": "builder",
                        "version": "1.0",
                        "usages": ["build-only"],
                        "presence": "unknown",
                    }
                ],
            },
        )

        for payload in cases:
            with self.subTest(payload=payload):
                build = normalize_build_evidence(payload, "a" * 64)
                self.assertEqual("failed", build["status"])

    def test_declaration_patterns_map_declarations_without_claiming_file_ownership(self) -> None:
        dependency = component("pybind11", distribution="build-only")
        dependency["usages"][0]["path_patterns"] = ["vendor/pybind11/**"]
        dependency["usages"][0]["declaration_patterns"] = ["pyproject.toml"]
        result = reconcile_discoveries(
            [dependency],
            [
                {
                    "kind": "dependency-declaration",
                    "path": "pyproject.toml",
                }
            ],
        )
        self.assertEqual([], result["unmapped"])

    def test_dependency_diff_separates_addition_from_version_update(self) -> None:
        baseline = [component("triton", "1"), component("llvm", "1")]
        current = [
            component("triton", "2"),
            component("llvm", "1"),
            component("pybind11", "3"),
        ]
        difference = diff_components(baseline, current)
        self.assertEqual(["pybind11"], [entry["id"] for entry in difference["added"]])
        self.assertEqual([], difference["removed"])
        self.assertEqual(
            [
                {
                    "id": "triton",
                    "from_version": "1",
                    "to_version": "2",
                    "changed_fields": ["version"],
                }
            ],
            difference["updated"],
        )

    def test_notice_coverage_matches_only_distributed_components(self) -> None:
        components = [
            component("triton", distribution="embedded"),
            component("helper", distribution="bundled"),
            component("glibc", distribution="runtime-external"),
            component("setuptools", distribution="build-only"),
        ]
        entries = notice_entries(components)
        self.assertEqual({"triton", "helper"}, {entry["component_id"] for entry in entries})
        self.assertEqual([], validate_notice_coverage(components, entries))

        incomplete = [entry for entry in entries if entry["component_id"] != "helper"]
        findings = validate_notice_coverage(components, incomplete)
        self.assertEqual(
            [("missing-notice", "helper")],
            [(finding["code"], finding["component_id"]) for finding in findings],
        )


if __name__ == "__main__":
    unittest.main()
