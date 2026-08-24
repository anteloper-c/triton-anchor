"""Compatibility facade and artifact-level T8.2 compliance orchestration.

Policy, evidence discovery, shared model helpers, and release outputs live in
focused modules. Existing callers can continue importing the public API from
scripts.compliance.core.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Mapping, Sequence

from .discovery import (
    diff_components,
    normalize_build_evidence,
    normalize_cyclonedx,
    normalize_osv,
    normalize_scancode,
    reconcile_discoveries,
    wheel_discoveries,
)
from .model import (
    AUDITED_USAGE,
    BUILD_EVIDENCE_BINDINGS,
    PRODUCT_USAGE,
    SUPPORTED_SCHEMA_VERSION,
    USAGE_CATEGORIES,
    ComplianceDataError,
    load_json,
    stable_json,
    validate_registry,
    validate_target,
    write_json,
    _usage_categories,
    _usage_statuses,
)
from .policy import (
    REQUIRED_ACCEPTANCE_FIELDS,
    RESOLVED_VULNERABILITY_STATES,
    SEVERITY_ORDER,
    evaluate_dependency_admission,
    evaluate_dependency_relationships,
    evaluate_inventory_audit,
    evaluate_licenses,
    evaluate_vulnerabilities,
    evaluate_vulnerability_coverage,
    validate_policy,
    validate_risk_acceptances,
    _align_vulnerabilities,
    _component_resolution_findings,
)
from .release import (
    NOTICE_USAGE,
    artifact_links,
    artifact_sbom_link,
    evaluate_artifact_links,
    evaluate_notice_coverage,
    generate_sbom,
    notice_entries,
    render_notices,
    validate_notice_coverage,
)
__all__ = [
    "AUDITED_USAGE",
    "NOTICE_USAGE",
    "PRODUCT_USAGE",
    "REQUIRED_ACCEPTANCE_FIELDS",
    "RESOLVED_VULNERABILITY_STATES",
    "SEVERITY_ORDER",
    "SUPPORTED_SCHEMA_VERSION",
    "USAGE_CATEGORIES",
    "ComplianceDataError",
    "artifact_links",
    "artifact_sbom_link",
    "diff_components",
    "evaluate_artifact_links",
    "evaluate_artifact",
    "evaluate_candidate",
    "evaluate_dependency_admission",
    "evaluate_dependency_relationships",
    "evaluate_inventory_audit",
    "evaluate_licenses",
    "evaluate_notice_coverage",
    "evaluate_vulnerabilities",
    "evaluate_vulnerability_coverage",
    "generate_sbom",
    "load_json",
    "normalize_build_evidence",
    "normalize_cyclonedx",
    "normalize_osv",
    "normalize_scancode",
    "notice_entries",
    "reconcile_discoveries",
    "render_notices",
    "stable_json",
    "validate_notice_coverage",
    "validate_policy",
    "validate_registry",
    "validate_target",
    "validate_risk_acceptances",
    "wheel_discoveries",
    "write_json",
]


EVALUATION_CONTEXTS = {"technical-artifact", "formal-candidate"}
REQUIRED_EVIDENCE_BY_KIND = {
    "wheel": {
        "wheel",
        "scancode-source",
        "scancode-wheel",
        "syft",
        "osv",
        "build-evidence",
    },
    "github-source-snapshot": {
        "source-snapshot",
        "scancode-source",
        "syft",
        "dependency-inventory",
        "osv",
    },
}
SBOM_INVENTORY_EVIDENCE_BY_KIND = {
    kind: sources - {"osv"} for kind, sources in REQUIRED_EVIDENCE_BY_KIND.items()
}


def _source_notice_findings(
    artifact: Mapping[str, Any], canonical_notice: str
) -> list[dict[str, str]]:
    members = [
        member
        for member in artifact.get("files", [])
        if isinstance(member, Mapping)
        and member.get("path") == "THIRD_PARTY_NOTICES.md"
    ]
    if len(members) != 1:
        return [
            {
                "code": "source-notice-missing",
                "decision": "block",
                "reason": (
                    "source snapshot must distribute one root "
                    "THIRD_PARTY_NOTICES.md"
                ),
            }
        ]
    expected = canonical_notice.encode("utf-8")
    member = members[0]
    if member.get("size") != len(expected) or member.get("sha256") != (
        hashlib.sha256(expected).hexdigest()
    ):
        return [
            {
                "code": "source-notice-drift",
                "decision": "block",
                "reason": (
                    "source snapshot THIRD_PARTY_NOTICES.md does not match the "
                    "canonical component registry"
                ),
            }
        ]
    return []


def _evaluate_artifact(
    *,
    artifact: Mapping[str, Any],
    registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    discovery_reports: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    today: date,
    evaluation_context: str,
    vulnerability_coverage: Sequence[Mapping[str, Any]] = (),
    target: str = "core-wheel",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """Evaluate one concrete artifact without inferring release designation."""

    if evaluation_context not in EVALUATION_CONTEXTS:
        raise ComplianceDataError(
            f"evaluation_context must be one of {sorted(EVALUATION_CONTEXTS)}"
        )
    artifact_kind = str(artifact.get("artifact_kind", "wheel"))
    if artifact_kind not in REQUIRED_EVIDENCE_BY_KIND:
        raise ComplianceDataError(f"unsupported artifact kind: {artifact_kind}")
    required_evidence = REQUIRED_EVIDENCE_BY_KIND[artifact_kind]
    sbom_inventory_evidence = SBOM_INVENTORY_EVIDENCE_BY_KIND[artifact_kind]
    validate_registry(registry)
    validate_target(registry, target)
    validate_policy(policy)
    if isinstance(risk_acceptances, Mapping):
        validate_risk_acceptances(risk_acceptances)
    reconciliation = reconcile_discoveries(
        registry, discovery_reports, target=target
    )
    reports_by_source: dict[str, Mapping[str, Any]] = {}
    duplicate_sources: set[str] = set()
    for report in discovery_reports:
        source = str(report.get("source"))
        if source in reports_by_source:
            duplicate_sources.add(source)
        reports_by_source[source] = report
    successful_sources = {
        source
        for source, report in reports_by_source.items()
        if report.get("status", "success") == "success"
    }
    missing_sources = sorted(required_evidence - set(reports_by_source))
    failed_sources = sorted(
        source
        for source in required_evidence & set(reports_by_source)
        if source not in successful_sources
    )
    evidence_gaps = [
        {"source": source, "reason": "required evidence is missing"}
        for source in missing_sources
    ]
    evidence_gaps.extend(
        {"source": source, "reason": "required evidence did not succeed"}
        for source in failed_sources
    )
    evidence_gaps.extend(
        {"source": source, "reason": "required evidence source is duplicated"}
        for source in sorted(duplicate_sources)
    )
    source_representation_gap: dict[str, str] | None = None
    if artifact_kind == "github-source-snapshot":
        representations = artifact.get("representations")
        valid_representations = (
            isinstance(representations, list)
            and len(representations) == 2
            and {
                item.get("format")
                for item in representations
                if isinstance(item, Mapping)
            }
            == {"zip", "tar.gz"}
            and all(
                isinstance(item, Mapping)
                and isinstance(item.get("filename"), str)
                and bool(item["filename"])
                and isinstance(item.get("sha256"), str)
                and len(item["sha256"]) == 64
                and isinstance(item.get("size"), int)
                and item["size"] > 0
                for item in representations
            )
        )
        if not valid_representations:
            source_representation_gap = {
                "source": "source-snapshot",
                "reason": "source snapshot must identify its ZIP and tar.gz representations",
            }
            evidence_gaps.append(source_representation_gap)
        if artifact.get("source_identity_binding") not in {
            "verified-commit",
            "verified-tag-commit",
        }:
            evidence_gaps.append(
                {
                    "source": "source-snapshot",
                    "reason": "source snapshot is not bound to a verified Git tree",
                }
            )
    execution_issues = list(reconciliation["execution_issues"])
    execution_issues.extend(evidence_gaps[: len(missing_sources)])
    if source_representation_gap:
        execution_issues.append(source_representation_gap)
    execution_issues.extend(
        {
            "source": source,
            "code": "duplicate-evidence-source",
            "reason": "required evidence source is duplicated",
        }
        for source in sorted(duplicate_sources)
    )
    if reconciliation["unmapped"]:
        execution_issues.append(
            {
                "source": "reconciliation",
                "reason": (
                    f"{len(reconciliation['unmapped'])} discoveries are unmapped"
                ),
            }
        )

    build_report = reports_by_source.get("build-evidence", {})
    build_evidence_binding = (
        str(build_report.get("evidence_binding", "unknown"))
        if artifact_kind == "wheel"
        else "not-applicable"
    )
    if (
        artifact_kind == "wheel"
        and build_evidence_binding not in BUILD_EVIDENCE_BINDINGS
    ):
        evidence_gaps.append(
            {
                "source": "build-evidence",
                "reason": "normalized build evidence has an invalid binding",
            }
        )
        execution_issues.append(
            {
                "source": "build-evidence",
                "code": "invalid-evidence-binding",
                "reason": "normalized build evidence has an invalid binding",
            }
        )
    build_items = build_report.get("items", [])
    if not isinstance(build_items, list):
        build_items = []
    if (
        artifact_kind == "wheel"
        and build_report.get("status") == "success"
        and not any(
            isinstance(item, Mapping) and item.get("kind") == "build-component"
            for item in build_items
        )
    ):
        evidence_gaps.append(
            {
                "source": "build-evidence",
                "reason": "normalized build evidence has no build components",
            }
        )
        execution_issues.append(
            {
                "source": "build-evidence",
                "code": "empty-build-inventory",
                "reason": "normalized build evidence has no build components",
            }
        )
    if (
        artifact_kind == "wheel"
        and build_report.get("status") == "success"
        and build_report.get("artifact_sha256") != artifact.get("sha256")
    ):
        evidence_gaps.append(
            {
                "source": "build-evidence",
                "reason": "normalized build evidence is not bound to this Wheel SHA256",
            }
        )
        execution_issues.append(
            {
                "source": "build-evidence",
                "code": "artifact-binding-mismatch",
                "reason": "normalized build evidence is not bound to this Wheel SHA256",
            }
        )

    components = reconciliation["components"]
    assessed_components = [
        component
        for component in components
        if (
            active_categories := _usage_categories(component, target, active=True)
        )
        & (PRODUCT_USAGE | {"build-only"})
        and not all(
            _usage_statuses(component, target, category) == {"confirmed-optional"}
            for category in active_categories
        )
    ]
    license_findings = evaluate_licenses(assessed_components, policy)
    # Findings for components reviewed as test-only/CI-only are outside this
    # assessed artifact's product scope. Unknown components still fail closed.
    artifact_out_of_scope_ids = {
        str(component["id"])
        for component in components
        if (declared := _usage_categories(component, target))
        and declared <= {"test-only", "CI-only"}
    }
    aligned_vulnerabilities, vulnerability_alignment_issues = (
        _align_vulnerabilities(
            vulnerabilities,
            assessed_components,
            ignored_component_ids=artifact_out_of_scope_ids,
        )
    )
    execution_issues.extend(vulnerability_alignment_issues)
    vulnerability_findings = evaluate_vulnerabilities(
        aligned_vulnerabilities,
        risk_acceptances,
        str(artifact["sha256"]),
        today,
        str(policy.get("vulnerability_threshold", "high")),
        str(artifact.get("version")) if artifact.get("version") else None,
        target,
    )
    vulnerability_coverage_findings = evaluate_vulnerability_coverage(
        components, vulnerability_coverage, target
    )
    canonical_notices = notice_entries(registry, None)
    canonical_notice = render_notices(canonical_notices)
    notice_findings = validate_notice_coverage(
        components,
        canonical_notices,
        target,
        allow_extra=True,
    )
    if artifact_kind == "github-source-snapshot":
        notice_findings.extend(_source_notice_findings(artifact, canonical_notice))
    component_findings = _component_resolution_findings(components, target)
    dependency_findings = evaluate_dependency_relationships(components, target)
    policy_ready = policy.get("status") == "approved"

    compliance_blockers = [
        *license_findings,
        *vulnerability_findings,
        *vulnerability_coverage_findings,
        *notice_findings,
        *[finding for finding in component_findings if finding["decision"] != "allow"],
        *dependency_findings,
    ]
    if not policy_ready:
        compliance_blockers.append(
            {"decision": "block", "reason": "license and vulnerability policy is not approved"}
        )
    execution_status = "pass" if not execution_issues else "fail"
    evidence_status = "complete" if not evidence_gaps else "incomplete"
    compliance_status = "pass" if not compliance_blockers else "fail"
    promotion_findings = []
    if evaluation_context == "formal-candidate":
        if artifact_kind == "wheel" and build_evidence_binding != "same-build":
            promotion_findings.append(
                {
                    "code": "candidate-build-binding-not-established",
                    "decision": "block-promotion",
                    "reason": "formal candidate build evidence must declare same-build binding",
                }
            )
        if artifact_kind == "github-source-snapshot" and (
            artifact.get("reference_kind") != "tag"
            or artifact.get("source_identity_binding") != "verified-tag-commit"
        ):
            promotion_findings.append(
                {
                    "code": "candidate-source-tag-binding-not-established",
                    "decision": "block-promotion",
                    "reason": (
                        "formal source candidate must be a locally verified tag-to-commit snapshot"
                    ),
                }
            )
    if evaluation_context == "technical-artifact":
        promotion_status = "not-applicable"
    else:
        promotion_status = (
            "pass"
            if execution_status == compliance_status == "pass"
            and not promotion_findings
            else "blocked"
        )
    sbom_inventory_blockers = [
        *[
            finding
            for finding in component_findings
            if finding["decision"] != "allow"
        ],
        *dependency_findings,
    ]
    inventory_execution_issues = [
        issue
        for issue in reconciliation["execution_issues"]
        if issue.get("source") in sbom_inventory_evidence
    ]
    sbom_inventory_complete = (
        sbom_inventory_evidence <= successful_sources
        and not any(
            gap["source"] in sbom_inventory_evidence
            for gap in evidence_gaps
        )
        and not inventory_execution_issues
        and not reconciliation["unmapped"]
        and not sbom_inventory_blockers
    )
    sbom = generate_sbom(
        artifact,
        components,
        target,
        complete=sbom_inventory_complete,
    )
    link = artifact_sbom_link(artifact, sbom)
    report = {
        "schema_version": 1,
        "artifact": link["artifact"],
        "evaluation_context": evaluation_context,
        "artifact_evidence_binding": (
            build_evidence_binding
            if artifact_kind == "wheel"
            else artifact.get("source_identity_binding", "unverified")
        ),
        "build_evidence_binding": build_evidence_binding,
        "execution_status": execution_status,
        "evidence_status": evidence_status,
        "compliance_status": compliance_status,
        "promotion_status": promotion_status,
        "sbom_inventory_status": (
            "complete" if sbom_inventory_complete else "incomplete"
        ),
        "execution_issues": execution_issues,
        "evidence_gaps": evidence_gaps,
        "unmapped_discoveries": reconciliation["unmapped"],
        "excluded_discoveries": reconciliation["excluded"],
        "license_findings": license_findings,
        "vulnerability_findings": vulnerability_findings,
        "vulnerability_coverage_findings": vulnerability_coverage_findings,
        "notice_findings": notice_findings,
        "component_findings": component_findings,
        "dependency_findings": dependency_findings,
        "compliance_blockers": compliance_blockers,
        "promotion_findings": promotion_findings,
    }
    return report, sbom, link, canonical_notice


def evaluate_artifact(
    *,
    artifact: Mapping[str, Any],
    registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    discovery_reports: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    today: date,
    vulnerability_coverage: Sequence[Mapping[str, Any]] = (),
    target: str = "core-wheel",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """Technically evaluate a Wheel without granting promotion semantics."""

    return _evaluate_artifact(
        artifact=artifact,
        registry=registry,
        policy=policy,
        risk_acceptances=risk_acceptances,
        discovery_reports=discovery_reports,
        vulnerabilities=vulnerabilities,
        today=today,
        vulnerability_coverage=vulnerability_coverage,
        target=target,
        evaluation_context="technical-artifact",
    )


def evaluate_candidate(
    *,
    artifact: Mapping[str, Any],
    registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    discovery_reports: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    today: date,
    vulnerability_coverage: Sequence[Mapping[str, Any]] = (),
    target: str = "core-wheel",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """Evaluate an artifact explicitly designated by a trusted release caller."""

    return _evaluate_artifact(
        artifact=artifact,
        registry=registry,
        policy=policy,
        risk_acceptances=risk_acceptances,
        discovery_reports=discovery_reports,
        vulnerabilities=vulnerabilities,
        today=today,
        vulnerability_coverage=vulnerability_coverage,
        target=target,
        evaluation_context="formal-candidate",
    )
