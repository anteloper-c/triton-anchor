"""NOTICE generation, SBOM generation, and artifact/SBOM linkage."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from .model import (
    PRODUCT_USAGE,
    ComplianceDataError,
    _component_list,
    _effective_version,
    _usage_categories,
    stable_json,
)


NOTICE_USAGE = {"distributed", "embedded"}
_EXACT_VERSION_STATUS = {
    "confirmed",
    "observed",
    "pinned",
    "resolved",
    "resolved-gitlink",
}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._:-]+")

def notice_entries(
    components: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    target: str | None = "core-wheel",
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for component in _component_list(components):
        if not component.get("third_party", True):
            continue
        if not (
            _usage_categories(
                component, target, active="active_usages" in component
            )
            & NOTICE_USAGE
        ):
            continue
        license_info = component.get("license", {})
        origin = component.get("origin", {})
        version, version_status = _effective_version(component)
        entries.append(
            {
                "component_id": component["id"],
                "name": component.get("name"),
                "version": version,
                "version_status": version_status,
                "origin": origin.get("url") if isinstance(origin, Mapping) else origin,
                "origin_status": origin.get("status") if isinstance(origin, Mapping) else "unknown",
                "license": license_info.get("concluded") if isinstance(license_info, Mapping) else None,
                "license_status": license_info.get("status") if isinstance(license_info, Mapping) else "unknown",
                "license_text": license_info.get("text_location")
                if isinstance(license_info, Mapping)
                else None,
                "copyrights": sorted(set(component.get("copyrights", []))),
            }
        )
    return sorted(entries, key=lambda item: str(item["component_id"]))


def evaluate_notice_coverage(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        component_id = str(entry.get("component_id", ""))
        missing = [
            field
            for field in (
                "component_id",
                "name",
                "version",
                "origin",
                "license",
                "license_text",
            )
            if entry.get(field) in (None, "", [])
        ]
        if component_id in seen:
            missing.append("unique component_id")
        seen.add(component_id)
        if entry.get("version_status") not in {"confirmed", "resolved", "observed", "pinned"}:
            missing.append("confirmed version")
        if entry.get("origin_status") not in {"confirmed", "resolved"}:
            missing.append("confirmed origin")
        if entry.get("license_status") not in {"approved", "confirmed", "resolved"}:
            missing.append("approved license")
        if not entry.get("copyrights"):
            missing.append("copyright attribution")
        findings.append(
            {
                "component_id": component_id,
                "decision": "block" if missing else "allow",
                "missing": sorted(set(missing)),
            }
        )
    return findings


def validate_notice_coverage(
    components: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    target: str | None = "core-wheel",
    *,
    allow_extra: bool = False,
) -> list[dict[str, Any]]:
    """Return only Notice blockers, including entirely missing entries."""

    required = {entry["component_id"]: entry for entry in notice_entries(components, target)}
    supplied = {str(entry.get("component_id")): entry for entry in entries}
    findings: list[dict[str, Any]] = []
    for component_id in sorted(required.keys() - supplied.keys()):
        findings.append(
            {"code": "missing-notice", "component_id": component_id, "decision": "block"}
        )
    required_entries = [
        supplied[component_id]
        for component_id in sorted(required.keys() & supplied.keys())
    ]
    for finding in evaluate_notice_coverage(required_entries):
        if finding["decision"] != "allow":
            findings.append({"code": "incomplete-notice", **finding})
    if not allow_extra:
        for component_id in sorted(supplied.keys() - required.keys()):
            findings.append(
                {
                    "code": "unexpected-notice",
                    "component_id": component_id,
                    "decision": "block",
                }
            )
    return findings


def render_notices(entries: Sequence[Mapping[str, Any]]) -> str:
    def display(value: Any) -> str:
        return str(value) if value not in (None, "", []) else "UNRESOLVED"

    lines = [
        "# Third-Party Notices",
        "",
        "This canonical file is generated from the component registry. Do not edit entries by hand.",
        "This file is complete only when every required attribution field is resolved.",
        "",
    ]
    for entry in sorted(entries, key=lambda item: str(item.get("component_id", ""))):
        lines.extend(
            [
                f"## {entry.get('name')}",
                "",
                f"- Component ID: `{entry.get('component_id')}`",
                f"- Version: `{display(entry.get('version'))}`",
                f"- Source: {display(entry.get('origin'))}",
                f"- Concluded license: `{display(entry.get('license'))}`",
                f"- License text: `{display(entry.get('license_text'))}`",
                "- Copyright: "
                + display("; ".join(entry.get("copyrights", []))),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _bom_ref(component: Mapping[str, Any]) -> str:
    version, _ = _effective_version(component)
    safe_id = _SAFE_ID.sub("-", str(component["id"]))
    return f"component:{safe_id}@{version or 'unknown'}"


def _sbom_purl(component: Mapping[str, Any]) -> str | None:
    purl = component.get("purl")
    if not isinstance(purl, str) or not purl:
        return None
    version, version_status = _effective_version(component)
    if version and version_status in _EXACT_VERSION_STATUS:
        return f"{purl}@{quote(version, safe='')}"
    return purl


def _bom_component(component: Mapping[str, Any], target: str = "core-wheel") -> dict[str, Any]:
    version, _ = _effective_version(component)
    license_info = component.get("license", {})
    categories = sorted(
        _usage_categories(component, target, active="active_usages" in component)
    )
    active_usages = [
        usage
        for usage in component.get("usages", [])
        if isinstance(usage, Mapping)
        and usage.get("target") in (None, "*", target)
        and usage.get("category") in categories
    ]
    optional = bool(active_usages) and all(
        usage.get("status") == "confirmed-optional" for usage in active_usages
    )
    value: dict[str, Any] = {
        "bom-ref": _bom_ref(component),
        "type": component.get("type", component.get("kind", "library")),
        "name": component.get("name", component["id"]),
        "version": version or "unknown",
        "properties": [
            {"name": "triton-anchor:component-id", "value": str(component["id"])},
            {"name": "triton-anchor:usages", "value": ",".join(categories)},
        ],
    }
    if optional:
        value["scope"] = "optional"
    purl = _sbom_purl(component)
    if purl:
        value["purl"] = purl
    concluded = license_info.get("concluded") if isinstance(license_info, Mapping) else None
    if concluded:
        value["licenses"] = [{"expression": concluded}]
    origin = component.get("origin")
    url = origin.get("url") if isinstance(origin, Mapping) else origin
    if url:
        value["externalReferences"] = [{"type": "vcs", "url": url}]
    return value


def generate_sbom(
    artifact: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    target: str = "core-wheel",
    complete: bool = False,
) -> dict[str, Any]:
    canonical = _component_list(components)
    product = [
        component
        for component in canonical
        if _usage_categories(
            component, target, active="active_usages" in component
        )
        & PRODUCT_USAGE
        and component.get("third_party", True)
    ]
    build = [
        component
        for component in canonical
        if "build-only"
        in _usage_categories(
            component, target, active="active_usages" in component
        )
        and not (
            _usage_categories(
                component, target, active="active_usages" in component
            )
            & PRODUCT_USAGE
        )
        and component.get("third_party", True)
    ]
    root_ref = f"artifact:sha256:{artifact['sha256']}"
    product_refs = {_bom_ref(component): component for component in product}
    by_id = {str(component["id"]): component for component in product}
    root_component = next(
        (
            component
            for component in canonical
            if not component.get("third_party", True)
            and component.get("id") == "triton-anchor"
        ),
        None,
    )
    root_dependency_ids = (
        set(root_component.get("depends_on", []))
        if root_component and "depends_on" in root_component
        else set(by_id)
    )
    dependencies = [
        {
            "ref": root_ref,
            "dependsOn": sorted(
                _bom_ref(by_id[component_id])
                for component_id in root_dependency_ids
                if component_id in by_id
            ),
        }
    ]
    for ref, component in sorted(product_refs.items()):
        child_ids = set(component.get("depends_on", []))
        child_refs = sorted(
            _bom_ref(candidate) for candidate in product if candidate["id"] in child_ids
        )
        dependencies.append({"ref": ref, "dependsOn": child_refs})
    artifact_kind = str(artifact.get("artifact_kind", "wheel"))
    if artifact_kind == "github-source-snapshot":
        serial_identity = stable_json(
            {
                "artifact_kind": artifact_kind,
                "repository": artifact.get("repository"),
                "reference_kind": artifact.get("reference_kind"),
                "source_reference": artifact.get("source_reference"),
                "source_commit": artifact.get("source_commit"),
                "version": artifact.get("version"),
                "tree_sha256": artifact.get("sha256"),
            }
        )
    else:
        serial_identity = str(artifact["sha256"])
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"triton-anchor:{serial_identity}")
    properties = [
        {"name": "triton-anchor:artifact-kind", "value": artifact_kind},
        {
            "name": "triton-anchor:artifact-filename",
            "value": str(artifact["filename"]),
        },
    ]
    if artifact_kind == "github-source-snapshot":
        properties.extend(
            [
                {
                    "name": "triton-anchor:hash-scope",
                    "value": str(
                        artifact.get("hash_scope", "normalized-archive-content-tree")
                    ),
                },
                {
                    "name": "triton-anchor:source-repository",
                    "value": str(artifact["repository"]),
                },
                {
                    "name": "triton-anchor:source-reference-kind",
                    "value": str(artifact["reference_kind"]),
                },
                {
                    "name": "triton-anchor:source-reference",
                    "value": str(artifact["source_reference"]),
                },
                {
                    "name": "triton-anchor:source-commit",
                    "value": str(artifact["source_commit"]),
                },
                {
                    "name": "triton-anchor:source-identity-binding",
                    "value": str(
                        artifact.get("source_identity_binding", "unverified")
                    ),
                },
            ]
        )
    else:
        properties.extend(
            [
                {
                    "name": "triton-anchor:python-tag",
                    "value": str(
                        artifact.get("python_tag", artifact.get("tag", "unknown"))
                    ),
                },
                {
                    "name": "triton-anchor:abi-tag",
                    "value": str(artifact.get("abi_tag", "unknown")),
                },
                {
                    "name": "triton-anchor:platform-tag",
                    "value": str(
                        artifact.get("platform_tag", artifact.get("tag", "unknown"))
                    ),
                },
            ]
        )
    bom: dict[str, Any] = {
        "$schema": "http://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "type": "application",
                "name": artifact.get("name", "triton-anchor"),
                "version": artifact["version"],
                "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
                "properties": properties,
            }
        },
        "components": [
            _bom_component(component, target)
            for component in sorted(product, key=lambda item: item["id"])
        ],
        "dependencies": dependencies,
        "compositions": [
            {
                "aggregate": "complete" if complete else "incomplete",
                "assemblies": [root_ref],
            }
        ],
    }
    if build:
        bom["formulation"] = [
            {
                "bom-ref": f"formulation:{artifact['sha256']}",
                "components": [
                    _bom_component(component, target)
                    for component in sorted(build, key=lambda item: item["id"])
                ],
            }
        ]
    return bom


def artifact_sbom_link(artifact: Mapping[str, Any], sbom: Mapping[str, Any]) -> dict[str, Any]:
    metadata_component = sbom.get("metadata", {}).get("component", {})
    if metadata_component.get("version") != artifact.get("version"):
        raise ComplianceDataError("SBOM root version does not match the assessed artifact")
    hashes = {
        item.get("alg"): item.get("content")
        for item in metadata_component.get("hashes", [])
        if isinstance(item, Mapping)
    }
    if hashes.get("SHA-256") != artifact.get("sha256"):
        raise ComplianceDataError("SBOM root hash does not identify the assessed artifact")
    sbom_bytes = stable_json(sbom).encode("utf-8")
    artifact_kind = str(artifact.get("artifact_kind", "wheel"))
    artifact_identity: dict[str, Any] = {
        "artifact_kind": artifact_kind,
        "filename": artifact["filename"],
        "name": artifact.get("name", "triton-anchor"),
        "version": artifact["version"],
        "sha256": artifact["sha256"],
    }
    if artifact_kind == "github-source-snapshot":
        artifact_identity.update(
            {
                "hash_scope": artifact.get(
                    "hash_scope", "normalized-archive-content-tree"
                ),
                "repository": artifact["repository"],
                "reference_kind": artifact["reference_kind"],
                "source_reference": artifact["source_reference"],
                "source_commit": artifact["source_commit"],
                "source_identity_binding": artifact.get(
                    "source_identity_binding", "unverified"
                ),
                "representations": [
                    dict(representation)
                    for representation in artifact.get("representations", [])
                ],
                "gitlinks": [
                    dict(gitlink) for gitlink in artifact.get("gitlinks", [])
                ],
            }
        )
    else:
        artifact_identity.update(
            {
                "python_tag": artifact.get(
                    "python_tag", artifact.get("tag", "unknown")
                ),
                "abi_tag": artifact.get("abi_tag", "unknown"),
                "platform_tag": artifact.get(
                    "platform_tag", artifact.get("tag", "unknown")
                ),
            }
        )
    return {
        "schema_version": 1,
        "artifact": artifact_identity,
        "sbom": {
            "format": "CycloneDX",
            "spec_version": sbom["specVersion"],
            "serial_number": sbom["serialNumber"],
            "sha256": hashlib.sha256(sbom_bytes).hexdigest(),
        },
    }
