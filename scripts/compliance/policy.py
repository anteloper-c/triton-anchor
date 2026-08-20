"""Compliance policy validation and release-gate decisions."""

from __future__ import annotations

import copy
from datetime import date
from typing import Any, Mapping, Sequence

from .discovery import diff_components, reconcile_discoveries
from .model import (
    AUDITED_USAGE,
    PRODUCT_USAGE,
    USAGE_CATEGORIES,
    ComplianceDataError,
    _component_list,
    _effective_origin,
    _effective_version,
    _usage_categories,
    _usage_statuses,
    _validate_schema_version,
    validate_registry,
    validate_target,
)


SEVERITY_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RESOLVED_VULNERABILITY_STATES = {"fixed", "isolated", "upgraded"}
REQUIRED_ACCEPTANCE_FIELDS = {
    "id",
    "vulnerability_id",
    "component_id",
    "component_version",
    "status",
    "reason",
    "approved_by",
    "approved_on",
    "expires_on",
}
_NON_EXACT_VERSION_STATES = {
    "candidate-evidence-required",
    "conflict",
    "constraint-only",
    "pending",
    "unknown",
    "unresolved",
    "unresolved-conflict",
}

def validate_policy(policy: Mapping[str, Any]) -> None:
    _validate_schema_version(policy, "license policy")
    if policy.get("status") not in {"pending", "approved"}:
        raise ComplianceDataError("license policy status must be pending or approved")
    expressions = policy.get("expressions")
    if not isinstance(expressions, Mapping) or not expressions:
        raise ComplianceDataError("license policy expressions must be a non-empty object")
    for expression, entry in expressions.items():
        if not isinstance(expression, str) or not expression:
            raise ComplianceDataError("license policy expression keys must be non-empty")
        if not isinstance(entry, Mapping) or entry.get("decision") not in {
            "allow",
            "deny",
            "review-required",
        }:
            raise ComplianceDataError(
                f"license policy expression {expression!r} has an invalid decision"
            )
    threshold = str(policy.get("vulnerability_threshold", "")).lower()
    if threshold not in SEVERITY_ORDER:
        raise ComplianceDataError("license policy has an invalid vulnerability threshold")


def validate_risk_acceptances(config: Mapping[str, Any]) -> None:
    _validate_schema_version(config, "risk acceptances")
    records = config.get("records")
    if not isinstance(records, list) or any(
        not isinstance(record, Mapping) for record in records
    ):
        raise ComplianceDataError("risk acceptances records must be an array of objects")
    ids: set[str] = set()
    for record in records:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ComplianceDataError("Every risk acceptance must have a non-empty id")
        if record_id in ids:
            raise ComplianceDataError(f"Duplicate risk acceptance id: {record_id}")
        ids.add(record_id)
        if record.get("status") == "accepted":
            missing = sorted(
                field for field in REQUIRED_ACCEPTANCE_FIELDS if record.get(field) in (None, "")
            )
            if missing:
                raise ComplianceDataError(
                    f"Risk acceptance {record_id} is missing fields: {', '.join(missing)}"
                )
            if not record.get("artifact_sha256") and not record.get("release_version"):
                raise ComplianceDataError(
                    f"Risk acceptance {record_id} needs artifact_sha256 or release_version scope"
                )

def evaluate_dependency_admission(
    *,
    baseline_registry: Mapping[str, Any],
    current_registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    declaration_delta_reports: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    vulnerability_coverage: Sequence[Mapping[str, Any]],
    today: date,
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]] = (),
    release_version: str | None = None,
    target: str = "core-wheel",
) -> dict[str, Any]:
    """Evaluate only dependency additions and policy-relevant updates.

    The caller supplies a semantic declaration delta.  A changed declaration
    that is not represented in the reviewed registry fails closed; unchanged
    dependencies are left to the scheduled full audit.
    """

    validate_registry(baseline_registry)
    validate_registry(current_registry)
    validate_target(baseline_registry, target)
    validate_target(current_registry, target)
    validate_policy(policy)
    if isinstance(risk_acceptances, Mapping):
        validate_risk_acceptances(risk_acceptances)
    difference = diff_components(baseline_registry, current_registry)
    reconciliation = reconcile_discoveries(current_registry, declaration_delta_reports)
    changed_ids = {
        str(component["id"]) for component in difference["added"]
    } | {str(change["id"]) for change in difference["updated"]}
    current_by_id = {
        str(component["id"]): component for component in _component_list(current_registry)
    }
    evidence_required_ids = {
        str(component["id"])
        for component in difference["added"]
        if component.get("third_party", True)
    }

    observed_changed_ids = {
        str(component_id)
        for item in reconciliation["mapped"]
        if item.get("kind") == "dependency-declaration"
        for component_id in item.get("component_ids", [])
        if str(component_id) in changed_ids
    }
    admission_findings: list[dict[str, Any]] = []
    for item in reconciliation["mapped"]:
        if item.get("kind") != "dependency-declaration":
            continue
        mapped_ids = {str(value) for value in item.get("component_ids", [])}
        if not (mapped_ids & changed_ids):
            admission_findings.append(
                {
                    "code": "dependency-declaration-without-reviewed-change",
                    "path": item.get("path"),
                    "name": item.get("name"),
                    "decision": "block",
                }
            )
    for component_id in sorted(evidence_required_ids - observed_changed_ids):
        admission_findings.append(
            {
                "code": "dependency-change-without-declaration-evidence",
                "component_id": component_id,
                "decision": "block",
            }
        )

    by_id = {
        str(component["id"]): component for component in reconciliation["components"]
    }
    changed_components = [by_id[component_id] for component_id in sorted(changed_ids)]
    for component in changed_components:
        if not component.get("third_party", True):
            continue
        version, version_status = _effective_version(component)
        origin, origin_status = _effective_origin(component)
        license_info = component.get("license", {})
        missing = []
        if not version or version_status in {"unknown", "unresolved", "conflict", "pending"}:
            missing.append("reviewable version or constraint")
        if not origin or origin_status not in {"confirmed", "resolved", "observed"}:
            missing.append("resolved origin")
        if (
            not isinstance(license_info, Mapping)
            or not license_info.get("concluded")
            or license_info.get("status") not in {"approved", "confirmed", "resolved"}
        ):
            missing.append("approved concluded license")
        if missing:
            admission_findings.append(
                {
                    "code": "dependency-metadata-incomplete",
                    "component_id": component["id"],
                    "decision": "block",
                    "missing": missing,
                }
            )

    license_findings = evaluate_licenses(changed_components, policy)
    # Admission owns only added or policy-relevant changed components. Findings
    # for known unchanged components remain audit scope; unknown identities fail.
    changed_vulnerabilities, vulnerability_alignment_issues = (
        _align_vulnerabilities(
            vulnerabilities,
            changed_components,
            ignored_component_ids=set(current_by_id) - changed_ids,
        )
    )
    vulnerability_findings = evaluate_vulnerabilities(
        changed_vulnerabilities,
        risk_acceptances,
        "",
        today,
        str(policy.get("vulnerability_threshold", "high")),
        release_version,
        target,
    )
    coverage_findings = evaluate_vulnerability_coverage(
        changed_components,
        vulnerability_coverage,
        target,
        USAGE_CATEGORIES,
        include_optional=True,
    )
    execution_issues = list(reconciliation["execution_issues"])
    execution_issues.extend(vulnerability_alignment_issues)
    if reconciliation["unmapped"]:
        execution_issues.append(
            {
                "source": "dependency-admission",
                "reason": f"{len(reconciliation['unmapped'])} dependency declarations are unmapped",
            }
        )
    blockers = [
        *admission_findings,
        *license_findings,
        *vulnerability_findings,
        *coverage_findings,
    ]
    if policy.get("status") != "approved":
        blockers.append(
            {
                "code": "policy-not-approved",
                "decision": "block",
            }
        )
    execution_status = "pass" if not execution_issues else "fail"
    compliance_status = "pass" if not blockers else "fail"
    return {
        "schema_version": 1,
        "execution_status": execution_status,
        "compliance_status": compliance_status,
        "admission_status": "pass"
        if execution_status == compliance_status == "pass"
        else "blocked",
        "difference": difference,
        "execution_issues": execution_issues,
        "unmapped_declarations": reconciliation["unmapped"],
        "admission_findings": admission_findings,
        "license_findings": license_findings,
        "vulnerability_findings": vulnerability_findings,
        "vulnerability_coverage_findings": coverage_findings,
        "blockers": blockers,
    }


def evaluate_inventory_audit(
    *,
    registry: Mapping[str, Any],
    policy: Mapping[str, Any],
    discovery_reports: Sequence[Mapping[str, Any]],
    vulnerabilities: Sequence[Mapping[str, Any]],
    vulnerability_coverage: Sequence[Mapping[str, Any]],
    today: date,
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]] = (),
    release_version: str | None = None,
    target: str = "core-wheel",
) -> dict[str, Any]:
    """Evaluate the full declared dependency inventory for scheduled audits."""

    validate_registry(registry)
    validate_target(registry, target)
    validate_policy(policy)
    if isinstance(risk_acceptances, Mapping):
        validate_risk_acceptances(risk_acceptances)
    reconciliation = reconcile_discoveries(registry, discovery_reports)
    components = reconciliation["components"]
    audit_inventory_components = [
        component
        for component in components
        if _usage_categories(component) & AUDITED_USAGE
    ]
    audited_components = [
        component
        for component in audit_inventory_components
        if component.get("third_party", True)
    ]
    inventory_findings: list[dict[str, Any]] = []
    for component in audited_components:
        version, version_status = _effective_version(component)
        origin, origin_status = _effective_origin(component)
        license_info = component.get("license", {})
        missing = []
        if not version or version_status in {"unknown", "unresolved", "conflict", "pending"}:
            missing.append("version or reviewed constraint")
        if not origin or origin_status not in {"confirmed", "resolved", "observed"}:
            missing.append("resolved origin")
        if (
            not isinstance(license_info, Mapping)
            or not license_info.get("concluded")
            or license_info.get("status") not in {"approved", "confirmed", "resolved"}
        ):
            missing.append("approved concluded license")
        if missing:
            inventory_findings.append(
                {
                    "code": "inventory-component-incomplete",
                    "component_id": component["id"],
                    "decision": "block",
                    "missing": missing,
                }
            )

    license_findings = evaluate_licenses(audited_components, policy)
    audit_coverage_components = copy.deepcopy(audited_components)
    for component in audit_coverage_components:
        component["active_usages"] = sorted(
            _usage_categories(component) & AUDITED_USAGE
        )
    coverage_findings = evaluate_vulnerability_coverage(
        audit_coverage_components,
        vulnerability_coverage,
        target,
        AUDITED_USAGE,
        include_optional=True,
    )
    aligned_vulnerabilities, vulnerability_alignment_issues = (
        _align_vulnerabilities(
            vulnerabilities,
            audit_inventory_components,
            ignored_component_ids={
                str(component["id"])
                for component in components
                if (categories := _usage_categories(component))
                and categories <= {"CI-only"}
            },
        )
    )
    vulnerability_findings = evaluate_vulnerabilities(
        aligned_vulnerabilities,
        risk_acceptances,
        "",
        today,
        str(policy.get("vulnerability_threshold", "high")),
        release_version,
        target,
    )
    execution_issues = list(reconciliation["execution_issues"])
    execution_issues.extend(vulnerability_alignment_issues)
    if reconciliation["unmapped"]:
        execution_issues.append(
            {
                "source": "inventory-audit",
                "reason": f"{len(reconciliation['unmapped'])} discoveries are unmapped",
            }
        )
    blockers = [
        *inventory_findings,
        *license_findings,
        *coverage_findings,
        *vulnerability_findings,
    ]
    if policy.get("status") != "approved":
        blockers.append(
            {"code": "policy-not-approved", "decision": "block"}
        )
    execution_status = "pass" if not execution_issues else "fail"
    compliance_status = "pass" if not blockers else "fail"
    return {
        "schema_version": 1,
        "execution_status": execution_status,
        "compliance_status": compliance_status,
        "audit_status": "pass"
        if execution_status == compliance_status == "pass"
        else "blocked",
        "execution_issues": execution_issues,
        "unmapped_discoveries": reconciliation["unmapped"],
        "excluded_discoveries": reconciliation["excluded"],
        "inventory_findings": inventory_findings,
        "license_findings": license_findings,
        "vulnerability_coverage_findings": coverage_findings,
        "vulnerability_findings": vulnerability_findings,
        "blockers": blockers,
    }


def _policy_decision(expression: str, policy: Mapping[str, Any]) -> str:
    exact = policy.get("expressions", {}) or policy.get("expression_decisions", {})
    if isinstance(exact, Mapping) and expression in exact:
        value = exact[expression]
        return str(value.get("decision")) if isinstance(value, Mapping) else str(value)
    if expression in policy.get("allowed_expressions", []):
        return "allow"
    if expression in policy.get("denied_expressions", []):
        return "deny"
    return "review-required"


def evaluate_licenses(
    components: Sequence[Mapping[str, Any]] | Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for component in _component_list(components):
        if not component.get("third_party", True):
            continue
        if not (_usage_categories(component) & AUDITED_USAGE):
            continue
        license_info = component.get("license") if isinstance(component.get("license"), Mapping) else {}
        concluded = license_info.get("concluded")
        status = license_info.get("status") or license_info.get("review_status")
        expressions = set()
        if concluded:
            expressions.add(str(concluded))
        for observation in component.get("observations", []):
            if observation.get("license_expression"):
                expressions.add(str(observation["license_expression"]))
        if not concluded or status not in {"approved", "confirmed", "resolved"}:
            findings.append(
                {
                    "code": "license-review-required",
                    "component_id": component["id"],
                    "expression": concluded,
                    "decision": "review-required",
                    "reason": "component has no approved concluded license",
                }
            )
        for expression in sorted(expressions):
            decision = _policy_decision(expression, policy)
            if decision != "allow":
                findings.append(
                    {
                        "code": "denied-license" if decision == "deny" else "license-review-required",
                        "component_id": component["id"],
                        "expression": expression,
                        "decision": decision,
                        "reason": "exact SPDX expression policy",
                    }
                )
    return findings


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ComplianceDataError(f"Risk acceptance {field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ComplianceDataError(f"Risk acceptance {field} must be an ISO date") from exc


def _valid_acceptance(
    record: Mapping[str, Any],
    vulnerability: Mapping[str, Any],
    artifact_sha256: str,
    artifact_version: str | None,
    target: str,
    today: date,
) -> tuple[bool, str]:
    missing = sorted(
        key for key in REQUIRED_ACCEPTANCE_FIELDS if record.get(key) in (None, "")
    )
    if missing:
        return False, f"risk acceptance missing fields: {', '.join(missing)}"
    if record["status"] != "accepted":
        return False, "risk acceptance status is not accepted"
    expected = {
        "vulnerability_id": vulnerability.get("id"),
        "component_id": vulnerability.get("component_id"),
        "component_version": vulnerability.get("component_version"),
    }
    for field, value in expected.items():
        if record.get(field) != value:
            return False, f"risk acceptance does not match {field}"
    if record.get("target") not in (None, "", target):
        return False, "risk acceptance does not match target"
    if record.get("artifact_sha256"):
        if record.get("artifact_sha256") != artifact_sha256:
            return False, "risk acceptance does not match artifact_sha256"
    elif record.get("release_version"):
        if not artifact_version or record.get("release_version") != artifact_version:
            return False, "risk acceptance does not match release_version"
    else:
        return False, "risk acceptance has no release or artifact scope"
    try:
        approved_on = _parse_date(record["approved_on"], "approved_on")
        expires_on = _parse_date(record["expires_on"], "expires_on")
    except ComplianceDataError as exc:
        return False, str(exc)
    if approved_on > today:
        return False, "risk acceptance approval date is in the future"
    if expires_on < today:
        return False, "risk acceptance expired"
    if expires_on < approved_on:
        return False, "risk acceptance expires before approval"
    return True, "approved risk acceptance"


def _vulnerability_inventory_mismatch(
    vulnerability: Mapping[str, Any],
    inventory_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    raw_component_id = vulnerability.get("component_id")
    component_id = (
        str(raw_component_id) if raw_component_id not in (None, "") else None
    )
    reported_version = vulnerability.get("component_version")
    component = inventory_by_id.get(component_id or "")
    expected_version: str | None = None
    version_status: str | None = None

    if component_id is None:
        reason = "OSV finding has no mapped component_id"
    elif component is None:
        reason = "OSV finding component is not present in the evaluated inventory"
    else:
        candidate_version, version_status = _effective_version(component)
        if (
            not candidate_version
            or version_status in _NON_EXACT_VERSION_STATES
        ):
            reason = "evaluated inventory has no resolved component version"
        else:
            expected_version = str(candidate_version)
            if reported_version in (None, ""):
                reason = "OSV finding has no component_version"
            elif str(reported_version) != expected_version:
                reason = (
                    "OSV finding component_version does not match evaluated inventory"
                )
            else:
                return None

    return {
        "code": "vulnerability-inventory-mismatch",
        "source": "osv",
        "id": vulnerability.get("id"),
        "component_id": component_id,
        "component_version": reported_version,
        "expected_component_version": expected_version,
        "inventory_version_status": version_status,
        "reason": reason,
    }


def _align_vulnerabilities(
    vulnerabilities: Sequence[Mapping[str, Any]],
    inventory_components: Sequence[Mapping[str, Any]],
    *,
    ignored_component_ids: set[str] | None = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    inventory_by_id = {
        str(component["id"]): component
        for component in _component_list(inventory_components)
    }
    ignored = ignored_component_ids or set()
    aligned: list[Mapping[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for vulnerability in vulnerabilities:
        component_id = vulnerability.get("component_id")
        if component_id not in (None, "") and str(component_id) in ignored:
            continue
        mismatch = _vulnerability_inventory_mismatch(
            vulnerability, inventory_by_id
        )
        if mismatch is None:
            aligned.append(vulnerability)
        else:
            issues.append(mismatch)
    return aligned, issues


def evaluate_vulnerabilities(
    vulnerabilities: Sequence[Mapping[str, Any]],
    risk_acceptances: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    artifact_sha256: str,
    today: date,
    threshold: str = "high",
    artifact_version: str | None = None,
    target: str = "core-wheel",
) -> list[dict[str, Any]]:
    records = (
        risk_acceptances.get("records", [])
        if isinstance(risk_acceptances, Mapping)
        else risk_acceptances
    )
    findings: list[dict[str, Any]] = []
    threshold_value = SEVERITY_ORDER.get(threshold.lower())
    if threshold_value is None:
        raise ComplianceDataError(f"Unknown vulnerability threshold: {threshold}")
    for vulnerability in vulnerabilities:
        severity = str(vulnerability.get("severity", "unknown")).lower()
        if SEVERITY_ORDER.get(severity, 0) < threshold_value:
            continue
        state = str(vulnerability.get("status", "open")).lower()
        if state in RESOLVED_VULNERABILITY_STATES:
            if vulnerability.get("resolution_evidence"):
                decision, reason = "allow", f"vulnerability is {state} with evidence"
            else:
                decision, reason = "block", f"vulnerability is {state} but has no disposition evidence"
        else:
            matching_reasons: list[str] = []
            accepted = False
            for record in records:
                valid, reason = _valid_acceptance(
                    record,
                    vulnerability,
                    artifact_sha256,
                    artifact_version,
                    target,
                    today,
                )
                matching_reasons.append(reason)
                if valid:
                    accepted = True
                    break
            decision = "allow" if accepted else "block"
            reason = "approved risk acceptance" if accepted else (
                matching_reasons[0] if matching_reasons else "severe vulnerability is unresolved"
            )
        if decision != "allow":
            findings.append(
                {
                    "code": "unresolved-severe-vulnerability",
                    "id": vulnerability.get("id"),
                    "component_id": vulnerability.get("component_id"),
                    "component_version": vulnerability.get("component_version"),
                    "severity": severity,
                    "decision": decision,
                    "reason": reason,
                }
            )
    return findings

def evaluate_vulnerability_coverage(
    components: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
    target: str,
    required_usages: set[str] | None = None,
    *,
    include_optional: bool = False,
) -> list[dict[str, Any]]:
    required_categories = required_usages or (PRODUCT_USAGE | {"build-only"})
    by_component: dict[str, list[Mapping[str, Any]]] = {}
    for entry in coverage:
        by_component.setdefault(str(entry.get("component_id", "")), []).append(entry)
    findings: list[dict[str, Any]] = []
    for component in _component_list(components):
        if not component.get("third_party", True):
            continue
        active = _usage_categories(component, target, active=True)
        if not (active & required_categories):
            continue
        if not include_optional and active and all(
            _usage_statuses(component, target, category) == {"confirmed-optional"}
            for category in active
        ):
            continue
        version, version_status = _effective_version(component)
        runtime_constraint = component.get("runtime_constraint")
        constraint_only_runtime = bool(
            not version
            and runtime_constraint
            and active == {"runtime-external"}
        )
        coverage_identity = (
            version
            if version_status not in {
                "unknown",
                "pending",
                "conflict",
                "unresolved",
                "constraint-only",
                "candidate-evidence-required",
            }
            else None
        ) or (
            str(runtime_constraint) if constraint_only_runtime else None
        )
        if not coverage_identity:
            # Component resolution already blocks identities that have neither
            # a resolved version nor a reviewed runtime ABI constraint.  A
            # second coverage blocker would only duplicate that finding.
            continue
        valid = False
        reasons: list[str] = []
        for entry in by_component.get(str(component["id"]), []):
            if entry.get("component_version") != coverage_identity:
                reasons.append("coverage identity does not match candidate component")
                continue
            status = entry.get("status")
            if status == "scanned" and not constraint_only_runtime:
                required_scan_fields = (
                    "evidence",
                    "scanner",
                    "scanner_version",
                    "scanned_on",
                )
                if all(
                    entry.get(field) not in (None, "")
                    for field in required_scan_fields
                ):
                    valid = True
                    break
                reasons.append("scanned coverage is missing tool or result evidence")
                continue
            if status == "reviewed" and all(
                entry.get(field) not in (None, "")
                for field in ("evidence", "reviewed_by", "reviewed_on")
            ):
                valid = True
                break
            reasons.append(f"coverage status {status!r} is not sufficient")
        if not valid:
            findings.append(
                {
                    "code": "vulnerability-coverage-gap",
                    "component_id": component["id"],
                    "component_version": coverage_identity,
                    "decision": "block",
                    "reason": reasons[0]
                    if reasons
                    else (
                        "runtime ABI constraint requires reviewed vulnerability coverage"
                        if constraint_only_runtime
                        else "no vulnerability coverage evidence"
                    ),
                }
            )
    return findings

def evaluate_dependency_relationships(
    components: Sequence[Mapping[str, Any]], target: str
) -> list[dict[str, Any]]:
    canonical = _component_list(components)
    product = {
        str(component["id"]): component
        for component in canonical
        if component.get("third_party", True)
        and _usage_categories(
            component, target, active="active_usages" in component
        )
        & PRODUCT_USAGE
    }
    if not product:
        return []
    root = next(
        (
            component
            for component in canonical
            if component.get("id") == "triton-anchor"
            and not component.get("third_party", True)
        ),
        None,
    )
    if root is None or "depends_on" not in root:
        return [
            {
                "code": "missing-root-dependencies",
                "decision": "block",
                "reason": "the candidate root has no reviewed direct dependency set",
            }
        ]

    graph: dict[str, set[str]] = {
        "__root__": {
            str(component_id)
            for component_id in root.get("depends_on", [])
            if component_id in product
        }
    }
    graph.update(
        {
            component_id: {
                str(child)
                for child in component.get("depends_on", [])
                if child in product
            }
            for component_id, component in product.items()
        }
    )
    reachable: set[str] = set()
    pending = list(graph["__root__"])
    while pending:
        component_id = pending.pop()
        if component_id in reachable:
            continue
        reachable.add(component_id)
        pending.extend(sorted(graph.get(component_id, set()) - reachable))
    return [
        {
            "code": "unreachable-product-component",
            "component_id": component_id,
            "decision": "block",
            "reason": "component is present in the candidate but absent from the reviewed dependency graph",
        }
        for component_id in sorted(set(product) - reachable)
    ]

def _component_resolution_findings(components: Sequence[Mapping[str, Any]], target: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for component in components:
        declared = _usage_categories(component, target)
        active = _usage_categories(component, target, active=True)
        relevant = PRODUCT_USAGE | {"build-only"}
        if not component.get("third_party", True) or not (declared & relevant):
            continue
        required = {
            category
            for category in declared & relevant
            if "confirmed" in _usage_statuses(component, target, category)
        }
        missing = []
        for category in sorted(required - active):
            missing.append(
                {
                    "distributed": "candidate artifact evidence",
                    "embedded": "component-specific build/link evidence",
                    "runtime-external": "runtime requirement/linkage evidence",
                    "build-only": "build evidence",
                }[category]
            )
        candidate_classifications = {
            category
            for category in declared & relevant
            if "candidate-evidence-required"
            in _usage_statuses(component, target, category)
        }
        classified_absent = {
            str(category)
            for observation in component.get("observations", [])
            if observation.get("source") == "build-evidence"
            and observation.get("kind") == "build-component"
            and observation.get("presence") == "absent"
            for category in observation.get("candidate_usages", [])
        }
        if candidate_classifications and not (
            candidate_classifications & (active | classified_absent)
        ):
            missing.append("candidate usage classification")

        optional_only = bool(active) and all(
            _usage_statuses(component, target, category) == {"confirmed-optional"}
            for category in active
        )
        if not active or optional_only:
            findings.append(
                {
                    "component_id": component["id"],
                    "decision": "block" if missing else "allow",
                    "missing": sorted(set(missing)),
                }
            )
            continue
        version, version_status = _effective_version(component)
        origin_url, origin_status = _effective_origin(component)
        if not version or version_status in {
            "unknown",
            "pending",
            "conflict",
            "unresolved",
            "constraint-only",
            "candidate-evidence-required",
        }:
            if active != {"runtime-external"} or not component.get("runtime_constraint"):
                missing.append("resolved candidate version")
        if not origin_url or origin_status not in {"confirmed", "resolved"}:
            if origin_status != "observed":
                missing.append("resolved origin")
        findings.append(
            {
                "component_id": component["id"],
                "decision": "block" if missing else "allow",
                "missing": missing,
            }
        )
    return findings
