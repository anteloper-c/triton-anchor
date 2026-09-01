"""Command-line boundary for T8.2 compliance decisions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .core import (
    ComplianceDataError,
    evaluate_artifact,
    evaluate_candidate,
    evaluate_dependency_admission,
    evaluate_inventory_audit,
    load_json,
    normalize_build_evidence,
    normalize_cyclonedx,
    normalize_osv,
    normalize_scancode,
    notice_entries,
    render_notices,
    validate_notice_coverage,
    validate_policy,
    validate_registry,
    validate_risk_acceptances,
    validate_target,
    wheel_discoveries,
    write_json,
)
from .source_snapshot import inspect_source_snapshot, source_snapshot_discoveries
from .wheel import inspect_wheel


def _failed_artifact_report(
    evaluation_context: str, reason: str
) -> dict[str, Any]:
    """Return the stable report shape for an artifact that cannot be evaluated."""

    return {
        "schema_version": 1,
        "artifact": None,
        "evaluation_context": evaluation_context,
        "artifact_evidence_binding": "unknown",
        "build_evidence_binding": "unknown",
        "execution_status": "fail",
        "evidence_status": "incomplete",
        "compliance_status": "not-evaluated",
        "promotion_status": (
            "blocked"
            if evaluation_context == "formal-candidate"
            else "not-applicable"
        ),
        "sbom_inventory_status": "not-evaluated",
        "execution_issues": [{"source": "input", "reason": reason}],
        "evidence_gaps": [],
        "unmapped_discoveries": [],
        "excluded_discoveries": [],
        "license_findings": [],
        "vulnerability_findings": [],
        "vulnerability_coverage_findings": [],
        "notice_findings": [],
        "component_findings": [],
        "dependency_findings": [],
        "compliance_blockers": [],
        "promotion_findings": [],
    }


def _normalize_optional(
    path: str | None,
    source: str,
    normalizer: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if not path:
        return {
            "source": source,
            "status": "failed",
            "items": [],
            "issues": [f"{source} evidence is missing"],
        }
    try:
        return normalizer(load_json(path))
    except (OSError, ValueError) as exc:
        return {
            "source": source,
            "status": "failed",
            "items": [],
            "issues": [f"cannot read {source} evidence: {exc}"],
        }


def _normalize_dependency_inventory(report: Mapping[str, Any]) -> dict[str, Any]:
    raw_items = report.get("items")
    raw_issues = report.get("issues")
    issues: list[str] = []
    if not isinstance(raw_items, list):
        issues.append("dependency inventory items must be an array")
        raw_items = []
    if not isinstance(raw_issues, list):
        issues.append("dependency inventory issues must be an array")
        raw_issues = []
    items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    if len(items) != len(raw_items):
        issues.append("dependency inventory entries must be objects")
    if not items:
        issues.append("dependency inventory contains no declarations")
    issues.extend(str(issue) for issue in raw_issues)
    if report.get("status") != "success" and not raw_issues:
        issues.append("dependency inventory did not succeed")
    return {
        "source": "dependency-inventory",
        "status": "failed" if issues else "success",
        "items": items,
        "issues": issues,
    }


def _wheel_scan_evidence(
    artifact: Mapping[str, Any],
    scancode_path: str | None,
    syft_path: str | None,
) -> dict[str, Any] | None:
    """Bind raw Wheel scanner reports to the inspected artifact bytes."""

    inputs = (
        ("scancode-wheel", scancode_path),
        ("syft", syft_path),
    )
    if any(path is None for _, path in inputs):
        return None
    reports = []
    try:
        for source, value in inputs:
            path = Path(str(value))
            reports.append(
                {
                    "source": source,
                    "filename": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    except OSError:
        return None
    return {
        "artifact": {
            "filename": artifact["filename"],
            "sha256": artifact["sha256"],
        },
        "reports": reports,
    }


def _artifact_command(
    args: argparse.Namespace, evaluation_context: str
) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        source_options = (
            args.source_zip,
            args.source_tar,
            args.source_repository,
            args.source_reference_kind,
            args.source_reference,
            args.source_commit,
            args.source_version,
            args.source_repository_root,
        )
        if args.wheel and any(value is not None for value in source_options):
            raise ValueError("Wheel and source snapshot inputs cannot be combined")
        if args.wheel:
            artifact = inspect_wheel(args.wheel)
            target = args.target or "core-wheel"
        else:
            required_source = {
                "--source-zip": args.source_zip,
                "--source-tar": args.source_tar,
                "--source-repository": args.source_repository,
                "--source-reference-kind": args.source_reference_kind,
                "--source-reference": args.source_reference,
                "--source-commit": args.source_commit,
                "--source-version": args.source_version,
            }
            missing = [name for name, value in required_source.items() if not value]
            if missing:
                raise ValueError(
                    f"source snapshot input is missing: {', '.join(missing)}"
                )
            artifact = inspect_source_snapshot(
                args.source_zip,
                args.source_tar,
                repository=args.source_repository,
                reference_kind=args.source_reference_kind,
                reference=args.source_reference,
                commit_sha=args.source_commit,
                version=args.source_version,
                repository_root=args.source_repository_root,
            )
            target = args.target or "source-snapshot"
        registry = load_json(args.registry)
        policy = load_json(args.policy)
        risk_acceptances = load_json(args.risk_acceptances)
        validate_registry(registry)
        validate_target(registry, target)
        validate_policy(policy)
        validate_risk_acceptances(risk_acceptances)
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    except (OSError, ValueError) as exc:
        report = _failed_artifact_report(evaluation_context, str(exc))
        write_json(output_dir / "compliance-report.json", report)
        return 1

    scancode_source = _normalize_optional(
        args.scancode_source,
        "scancode-source",
        lambda report: normalize_scancode(report, "scancode-source", "source"),
    )
    syft = _normalize_optional(
        args.syft,
        "syft",
        lambda report: normalize_cyclonedx(report, "syft"),
    )
    osv_result = _normalize_optional(
        args.osv,
        "osv",
        lambda report: {
            "source": "osv",
            "items": [],
            **normalize_osv(report, registry.get("components", [])),
        },
    )
    vulnerabilities = osv_result.pop("vulnerabilities", [])
    vulnerability_coverage = osv_result.pop("coverage", [])
    if artifact["artifact_kind"] == "wheel":
        scancode_wheel = _normalize_optional(
            args.scancode_wheel,
            "scancode-wheel",
            lambda report: normalize_scancode(
                report, "scancode-wheel", "artifact"
            ),
        )
        build = _normalize_optional(
            args.build_evidence,
            "build-evidence",
            lambda report: normalize_build_evidence(
                report, str(artifact["sha256"])
            ),
        )
        discovery_reports = [
            wheel_discoveries(artifact),
            scancode_source,
            scancode_wheel,
            syft,
            build,
            osv_result,
        ]
    else:
        dependency_inventory = _normalize_optional(
            args.dependency_inventory,
            "dependency-inventory",
            _normalize_dependency_inventory,
        )
        discovery_reports = [
            source_snapshot_discoveries(artifact),
            scancode_source,
            syft,
            dependency_inventory,
            osv_result,
        ]
    evaluation_inputs = {
        "artifact": artifact,
        "registry": registry,
        "policy": policy,
        "risk_acceptances": risk_acceptances,
        "discovery_reports": discovery_reports,
        "vulnerabilities": vulnerabilities,
        "today": as_of,
        "vulnerability_coverage": vulnerability_coverage,
        "target": target,
    }
    if evaluation_context == "formal-candidate":
        report, sbom, link, canonical_notices = evaluate_candidate(
            **evaluation_inputs
        )
    else:
        report, sbom, link, canonical_notices = evaluate_artifact(
            **evaluation_inputs,
        )

    notice_issue = None
    if not args.notices:
        notice_issue = "canonical THIRD_PARTY_NOTICES.md was not provided"
    else:
        try:
            committed_notices = Path(args.notices).read_text(encoding="utf-8")
            if committed_notices.replace("\r\n", "\n") != canonical_notices:
                notice_issue = "canonical THIRD_PARTY_NOTICES.md is out of date"
        except OSError as exc:
            notice_issue = f"cannot read canonical THIRD_PARTY_NOTICES.md: {exc}"
    if notice_issue:
        report["notice_findings"].append(
            {"code": "notice-drift", "decision": "block", "reason": notice_issue}
        )
        report["compliance_blockers"].append(report["notice_findings"][-1])
        report["compliance_status"] = "fail"
        if evaluation_context == "formal-candidate":
            report["promotion_status"] = "blocked"

    if artifact["artifact_kind"] == "wheel":
        report["artifact_scan_evidence"] = _wheel_scan_evidence(
            artifact,
            args.scancode_wheel,
            args.syft,
        )

    sbom_name = f"{artifact['filename']}.cdx.json"
    link = copy.deepcopy(link)
    link["sbom"]["filename"] = sbom_name
    write_json(output_dir / sbom_name, sbom)
    write_json(output_dir / "artifact-sbom-link.json", link)
    write_json(output_dir / "compliance-report.json", report)
    (output_dir / "THIRD_PARTY_NOTICES.generated.md").write_text(
        canonical_notices, encoding="utf-8", newline="\n"
    )
    if evaluation_context == "formal-candidate":
        return 0 if report["promotion_status"] == "pass" else 1
    return 0 if (
        report["execution_status"] == "pass"
        and report["evidence_status"] == "complete"
        and report["compliance_status"] == "pass"
    ) else 1


def _artifact_evaluation_command(args: argparse.Namespace) -> int:
    return _artifact_command(args, "technical-artifact")


def _candidate_command(args: argparse.Namespace) -> int:
    return _artifact_command(args, "formal-candidate")


def _render_notices_command(args: argparse.Namespace) -> int:
    registry = load_json(args.registry)
    validate_registry(registry)
    if args.target is not None:
        validate_target(registry, args.target)
    entries = notice_entries(registry, args.target)
    findings = validate_notice_coverage(registry, entries, args.target)
    text = render_notices(entries)
    Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    if findings:
        for finding in findings:
            print(f"NOTICE blocker: {finding}", file=sys.stderr)
        return 1
    return 0


def _admission_command(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline_registry)
    current = load_json(args.registry)
    policy = load_json(args.policy)
    risk_acceptances = load_json(args.risk_acceptances)
    declaration_delta = load_json(args.declaration_delta)
    osv_result = _normalize_optional(
        args.osv,
        "osv",
        lambda report: {
            "source": "osv",
            "items": [],
            **normalize_osv(report, current.get("components", [])),
        },
    )
    vulnerabilities = osv_result.pop("vulnerabilities", [])
    coverage = osv_result.pop("coverage", [])
    report = evaluate_dependency_admission(
        baseline_registry=baseline,
        current_registry=current,
        policy=policy,
        declaration_delta_reports=[declaration_delta, osv_result],
        vulnerabilities=vulnerabilities,
        vulnerability_coverage=coverage,
        today=date.fromisoformat(args.as_of) if args.as_of else date.today(),
        risk_acceptances=risk_acceptances,
        release_version=args.release_version,
        target=args.target,
    )
    write_json(args.output, report)
    return 0 if report["admission_status"] == "pass" else 1


def _audit_command(args: argparse.Namespace) -> int:
    registry = load_json(args.registry)
    policy = load_json(args.policy)
    risk_acceptances = load_json(args.risk_acceptances)
    scancode = _normalize_optional(
        args.scancode,
        "scancode-source",
        lambda report: normalize_scancode(report, "scancode-source", "source"),
    )
    syft = _normalize_optional(
        args.syft,
        "syft",
        lambda report: normalize_cyclonedx(report, "syft"),
    )
    build = _normalize_optional(
        args.build_evidence,
        "build-evidence",
        normalize_build_evidence,
    )
    dependency_inventory = _normalize_optional(
        args.dependency_inventory,
        "dependency-inventory",
        _normalize_dependency_inventory,
    )
    osv_result = _normalize_optional(
        args.osv,
        "osv",
        lambda report: {
            "source": "osv",
            "items": [],
            **normalize_osv(report, registry.get("components", [])),
        },
    )
    vulnerabilities = osv_result.pop("vulnerabilities", [])
    coverage = osv_result.pop("coverage", [])
    report = evaluate_inventory_audit(
        registry=registry,
        policy=policy,
        discovery_reports=[
            scancode,
            syft,
            build,
            dependency_inventory,
            osv_result,
        ],
        vulnerabilities=vulnerabilities,
        vulnerability_coverage=coverage,
        today=date.fromisoformat(args.as_of) if args.as_of else date.today(),
        risk_acceptances=risk_acceptances,
        release_version=args.release_version,
        target=args.target,
    )
    write_json(args.output, report)
    return 0 if report["audit_status"] == "pass" else 1


def _add_artifact_arguments(command: argparse.ArgumentParser) -> None:
    artifact_input = command.add_mutually_exclusive_group(required=True)
    artifact_input.add_argument("--wheel")
    artifact_input.add_argument("--source-zip")
    command.add_argument("--source-tar")
    command.add_argument("--source-repository")
    command.add_argument(
        "--source-reference-kind", choices=("commit", "tag")
    )
    command.add_argument("--source-reference")
    command.add_argument("--source-commit")
    command.add_argument("--source-version")
    command.add_argument(
        "--source-repository-root",
        help="Git checkout used to verify the archive tree and tag/commit binding",
    )
    command.add_argument("--registry", required=True)
    command.add_argument("--policy", required=True)
    command.add_argument("--risk-acceptances", required=True)
    command.add_argument(
        "--scancode-source",
        help="ScanCode report for the source tree",
    )
    command.add_argument(
        "--scancode-wheel", help="ScanCode report for the unpacked Wheel"
    )
    command.add_argument("--syft")
    command.add_argument("--osv")
    command.add_argument("--build-evidence")
    command.add_argument("--dependency-inventory")
    command.add_argument("--notices")
    command.add_argument(
        "--target",
        default=None,
        help=(
            "logical artifact target (defaults to core-wheel for a Wheel and "
            "source-snapshot for GitHub source archives)"
        ),
    )
    command.add_argument(
        "--as-of", help="ISO date used to evaluate risk acceptance expiry"
    )
    command.add_argument("--output-dir", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    artifact_evaluation = subparsers.add_parser(
        "artifact-evaluation",
        help="technically evaluate one concrete artifact without promotion semantics",
    )
    _add_artifact_arguments(artifact_evaluation)
    artifact_evaluation.set_defaults(handler=_artifact_evaluation_command)

    candidate = subparsers.add_parser(
        "candidate",
        help="gate an artifact formally designated by the trusted release workflow",
    )
    _add_artifact_arguments(candidate)
    candidate.set_defaults(handler=_candidate_command)

    notices = subparsers.add_parser("render-notices", help="render the reviewed Notice registry")
    notices.add_argument("--registry", required=True)
    notices.add_argument(
        "--target",
        help="optional single target; omit it to render the canonical all-target Notice",
    )
    notices.add_argument("--output", required=True)
    notices.set_defaults(handler=_render_notices_command)

    admission = subparsers.add_parser(
        "admission", help="evaluate added or policy-relevant changed dependencies"
    )
    admission.add_argument("--baseline-registry", required=True)
    admission.add_argument("--registry", required=True)
    admission.add_argument("--policy", required=True)
    admission.add_argument("--risk-acceptances", required=True)
    admission.add_argument("--release-version")
    admission.add_argument("--declaration-delta", required=True)
    admission.add_argument("--osv", required=True)
    admission.add_argument("--target", default="core-wheel")
    admission.add_argument("--as-of")
    admission.add_argument("--output", required=True)
    admission.set_defaults(handler=_admission_command)

    audit = subparsers.add_parser(
        "audit", help="evaluate the full dependency inventory for scheduled CI"
    )
    audit.add_argument("--registry", required=True)
    audit.add_argument("--policy", required=True)
    audit.add_argument("--risk-acceptances", required=True)
    audit.add_argument("--release-version")
    audit.add_argument("--scancode", required=True)
    audit.add_argument("--syft", required=True)
    audit.add_argument("--osv", required=True)
    audit.add_argument("--build-evidence", required=True)
    audit.add_argument("--dependency-inventory", required=True)
    audit.add_argument("--target", default="core-wheel")
    audit.add_argument("--as-of")
    audit.add_argument("--output", required=True)
    audit.set_defaults(handler=_audit_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ComplianceDataError, OSError, ValueError) as exc:
        print(f"compliance error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
