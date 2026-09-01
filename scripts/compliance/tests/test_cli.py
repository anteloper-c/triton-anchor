from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.compliance.cli import (
    _audit_command,
    _normalize_dependency_inventory,
    build_parser,
    main,
)
from scripts.compliance.tests.helpers import component, make_wheel


class ComplianceCliTests(unittest.TestCase):
    def test_empty_dependency_inventory_is_not_complete_evidence(self) -> None:
        report = _normalize_dependency_inventory(
            {
                "source": "dependency-inventory",
                "status": "success",
                "items": [],
                "issues": [],
            }
        )

        self.assertEqual("failed", report["status"])

    def test_audit_consumes_the_generated_dependency_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry.json"
            policy = root / "policy.json"
            risk = root / "risk.json"
            output = root / "audit.json"
            evidence = root / "evidence.json"
            registry.write_text(
                json.dumps({"schema_version": 1, "components": []}),
                encoding="utf-8",
            )
            policy.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            risk.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            evidence.write_text("{}", encoding="utf-8")
            args = Namespace(
                registry=str(registry),
                policy=str(policy),
                risk_acceptances=str(risk),
                scancode=str(evidence),
                syft=str(evidence),
                build_evidence=str(evidence),
                dependency_inventory=str(evidence),
                osv=str(evidence),
                release_version=None,
                target="core-wheel",
                as_of=None,
                output=str(output),
            )

            def normalized(_path, source, _normalizer):
                result = {"source": source, "status": "success", "items": []}
                if source == "osv":
                    result.update(vulnerabilities=[], coverage=[])
                return result

            with patch(
                "scripts.compliance.cli._normalize_optional",
                side_effect=normalized,
            ), patch(
                "scripts.compliance.cli.evaluate_inventory_audit",
                return_value={"audit_status": "blocked"},
            ) as evaluate:
                exit_code = _audit_command(args)

        self.assertEqual(1, exit_code)
        sources = [
            report["source"]
            for report in evaluate.call_args.kwargs["discovery_reports"]
        ]
        self.assertIn("dependency-inventory", sources)

    def test_technical_evaluation_writes_outputs_without_promotion_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root)
            registry_path = root / "registry.json"
            policy_path = root / "policy.json"
            risk_path = root / "risk.json"
            scancode_path = root / "scancode-wheel.json"
            syft_path = root / "syft-wheel.cdx.json"
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
            scancode_path.write_text(
                json.dumps({"headers": [], "files": []}), encoding="utf-8"
            )
            syft_path.write_text(
                json.dumps(
                    {
                        "bomFormat": "CycloneDX",
                        "specVersion": "1.6",
                        "components": [],
                    }
                ),
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
                    "--scancode-wheel",
                    str(scancode_path),
                    "--syft",
                    str(syft_path),
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
            binding = report["artifact_scan_evidence"]
            self.assertEqual(
                report["artifact"]["sha256"],
                binding["artifact"]["sha256"],
            )
            self.assertEqual(
                {
                    "scancode-wheel": hashlib.sha256(
                        scancode_path.read_bytes()
                    ).hexdigest(),
                    "syft": hashlib.sha256(syft_path.read_bytes()).hexdigest(),
                },
                {item["source"]: item["sha256"] for item in binding["reports"]},
            )
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
                    "artifact_evidence_binding",
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
