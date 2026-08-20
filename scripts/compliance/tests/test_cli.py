from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.compliance.cli import build_parser, main
from scripts.compliance.tests.helpers import component, make_wheel


class ComplianceCliTests(unittest.TestCase):
    def test_artifact_evaluation_and_candidate_use_distinct_handlers(self) -> None:
        parser = build_parser()
        shared = [
            "--wheel",
            "artifact.whl",
            "--registry",
            "registry.json",
            "--policy",
            "policy.json",
            "--risk-acceptances",
            "risk.json",
            "--output-dir",
            "output",
        ]

        technical = parser.parse_args(["artifact-evaluation", *shared])
        candidate = parser.parse_args(["candidate", *shared])

        self.assertNotEqual(technical.handler, candidate.handler)

    def test_technical_evaluation_writes_outputs_without_promotion_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root)
            registry_path = root / "registry.json"
            policy_path = root / "policy.json"
            risk_path = root / "risk.json"
            output = root / "output"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "components": [
                            {
                                **component("demo", licenses=["MIT"]),
                                "third_party": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved",
                        "expressions": {"MIT": {"decision": "allow"}},
                        "vulnerability_threshold": "high",
                    }
                ),
                encoding="utf-8",
            )
            risk_path.write_text(
                json.dumps({"schema_version": 1, "records": []}),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "artifact-evaluation",
                    "--wheel",
                    str(wheel),
                    "--registry",
                    str(registry_path),
                    "--policy",
                    str(policy_path),
                    "--risk-acceptances",
                    str(risk_path),
                    "--output-dir",
                    str(output),
                ]
            )

            report = json.loads(
                (output / "compliance-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("technical-artifact", report["evaluation_context"])
            self.assertEqual("not-applicable", report["promotion_status"])
            self.assertEqual("incomplete", report["evidence_status"])
            self.assertTrue((output / "artifact-sbom-link.json").is_file())
            self.assertEqual(
                1,
                len(list(output.glob("*.whl.cdx.json"))),
            )

    def test_invalid_as_of_writes_a_fail_closed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root)
            registry_path = root / "registry.json"
            policy_path = root / "policy.json"
            risk_path = root / "risk.json"
            output = root / "output"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "components": [
                            {
                                **component("demo", licenses=["MIT"]),
                                "third_party": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved",
                        "expressions": {"MIT": {"decision": "allow"}},
                        "vulnerability_threshold": "high",
                    }
                ),
                encoding="utf-8",
            )
            risk_path.write_text(
                json.dumps({"schema_version": 1, "records": []}),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "artifact-evaluation",
                    "--wheel",
                    str(wheel),
                    "--registry",
                    str(registry_path),
                    "--policy",
                    str(policy_path),
                    "--risk-acceptances",
                    str(risk_path),
                    "--output-dir",
                    str(output),
                    "--as-of",
                    "not-a-date",
                ]
            )

            report = json.loads(
                (output / "compliance-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("technical-artifact", report["evaluation_context"])
            self.assertEqual("fail", report["execution_status"])
            self.assertEqual("not-applicable", report["promotion_status"])
            self.assertEqual("not-evaluated", report["sbom_inventory_status"])
            self.assertEqual(
                {
                    "artifact",
                    "build_evidence_binding",
                    "compliance_blockers",
                    "compliance_status",
                    "component_findings",
                    "dependency_findings",
                    "evaluation_context",
                    "evidence_gaps",
                    "evidence_status",
                    "excluded_discoveries",
                    "execution_issues",
                    "execution_status",
                    "license_findings",
                    "notice_findings",
                    "promotion_findings",
                    "promotion_status",
                    "sbom_inventory_status",
                    "schema_version",
                    "unmapped_discoveries",
                    "vulnerability_coverage_findings",
                    "vulnerability_findings",
                },
                set(report),
            )

    def test_unknown_artifact_target_fails_before_inventory_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root)
            registry_path = root / "registry.json"
            policy_path = root / "policy.json"
            risk_path = root / "risk.json"
            output = root / "output"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "components": [
                            {
                                **component("demo", licenses=["MIT"]),
                                "third_party": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved",
                        "expressions": {"MIT": {"decision": "allow"}},
                        "vulnerability_threshold": "high",
                    }
                ),
                encoding="utf-8",
            )
            risk_path.write_text(
                json.dumps({"schema_version": 1, "records": []}),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "artifact-evaluation",
                    "--wheel",
                    str(wheel),
                    "--registry",
                    str(registry_path),
                    "--policy",
                    str(policy_path),
                    "--risk-acceptances",
                    str(risk_path),
                    "--output-dir",
                    str(output),
                    "--target",
                    "linux-x86_64",
                ]
            )

            report = json.loads(
                (output / "compliance-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("fail", report["execution_status"])
            self.assertEqual("not-evaluated", report["sbom_inventory_status"])
            self.assertIn("not declared", report["execution_issues"][0]["reason"])

    def test_render_notices_rejects_an_unknown_logical_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "registry.json"
            output = root / "THIRD_PARTY_NOTICES.md"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "components": [
                            {
                                **component("demo", licenses=["MIT"]),
                                "third_party": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "render-notices",
                    "--registry",
                    str(registry_path),
                    "--target",
                    "linux-x86_64",
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(1, exit_code)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
