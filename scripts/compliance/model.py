"""Shared compliance data model, validation, and serialization helpers."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


PRODUCT_USAGE = {"distributed", "embedded", "runtime-external"}
AUDITED_USAGE = PRODUCT_USAGE | {"build-only", "test-only"}
USAGE_CATEGORIES = AUDITED_USAGE | {"CI-only"}
BUILD_EVIDENCE_BINDINGS = {"same-build", "post-hoc", "unknown"}
SUPPORTED_SCHEMA_VERSION = 1


class ComplianceDataError(ValueError):
    """Raised for malformed policy or evidence documents."""


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ComplianceDataError(f"Expected a JSON object in {path}")
    return value


def _validate_schema_version(document: Mapping[str, Any], name: str) -> None:
    if document.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ComplianceDataError(
            f"{name} schema_version must be {SUPPORTED_SCHEMA_VERSION}"
        )


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ComplianceDataError(f"{field} must be an array of non-empty strings")
    return value


def validate_registry(registry: Mapping[str, Any]) -> None:
    """Validate the fields consumed by reconciliation, SBOM, Notice, and gates."""

    _validate_schema_version(registry, "component registry")
    components = _component_list(registry)
    component_ids = {str(component["id"]) for component in components}
    for component in components:
        component_id = str(component["id"])
        if not isinstance(component.get("third_party"), bool):
            raise ComplianceDataError(
                f"Component {component_id} must declare third_party explicitly"
            )
        component_type = component.get("type", component.get("kind"))
        if not isinstance(component_type, str) or not component_type:
            raise ComplianceDataError(f"Component {component_id} must declare a type")
        for field in ("origin", "version", "license"):
            if not isinstance(component.get(field), Mapping):
                raise ComplianceDataError(
                    f"Component {component_id} {field} must be an object"
                )

        evidence = component.get("evidence", [])
        if not isinstance(evidence, list) or any(
            not isinstance(item, Mapping) for item in evidence
        ):
            raise ComplianceDataError(
                f"Component {component_id} evidence must be an array of objects"
            )
        evidence_ids = [item.get("id") for item in evidence]
        if any(not isinstance(item, str) or not item for item in evidence_ids):
            raise ComplianceDataError(
                f"Component {component_id} evidence entries need non-empty ids"
            )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ComplianceDataError(
                f"Component {component_id} contains duplicate evidence ids"
            )

        usages = component.get("usages")
        if not isinstance(usages, list) or not usages:
            raise ComplianceDataError(
                f"Component {component_id} must declare at least one usage"
            )
        references: list[str] = []
        for usage in usages:
            if not isinstance(usage, Mapping):
                raise ComplianceDataError(
                    f"Component {component_id} usage must be an object"
                )
            category = usage.get("category")
            if category not in USAGE_CATEGORIES:
                raise ComplianceDataError(
                    f"Component {component_id} has unknown usage category {category!r}"
                )
            if not isinstance(usage.get("target"), str) or not usage.get("target"):
                raise ComplianceDataError(
                    f"Component {component_id} usage must declare a target"
                )
            for pattern_field in (
                "path_patterns",
                "declaration_patterns",
                "artifact_patterns",
                "container_artifact_patterns",
            ):
                if pattern_field in usage:
                    _string_list(
                        usage[pattern_field],
                        f"Component {component_id} {pattern_field}",
                    )
            references.extend(
                _string_list(
                    usage.get("evidence_ids", []),
                    f"Component {component_id} usage evidence_ids",
                )
            )
        for section in ("version", "license"):
            references.extend(
                _string_list(
                    component[section].get("evidence_ids", []),
                    f"Component {component_id} {section} evidence_ids",
                )
            )
        dangling = sorted(set(references) - set(evidence_ids))
        if dangling:
            raise ComplianceDataError(
                f"Component {component_id} references unknown evidence ids: {dangling}"
            )

        dependencies = component.get("depends_on", [])
        _string_list(dependencies, f"Component {component_id} depends_on")
        invalid_dependencies = sorted(
            dependency
            for dependency in dependencies
            if dependency == component_id or dependency not in component_ids
        )
        if invalid_dependencies:
            raise ComplianceDataError(
                f"Component {component_id} has invalid dependencies: {invalid_dependencies}"
            )


def validate_target(registry: Mapping[str, Any], target: str) -> None:
    """Require an explicitly declared logical artifact target."""

    if not isinstance(target, str) or not target:
        raise ComplianceDataError("artifact target must be a non-empty string")
    declared_targets = {
        str(usage.get("target"))
        for component in _component_list(registry)
        if component.get("third_party") is False
        for usage in component.get("usages", [])
        if isinstance(usage, Mapping)
        and usage.get("category") in PRODUCT_USAGE
        and usage.get("target") not in (None, "*")
    }
    if target not in declared_targets:
        raise ComplianceDataError(
            f"artifact target {target!r} is not declared by a first-party product component"
        )

def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(stable_json(value), encoding="utf-8", newline="\n")


def _component_list(registry_or_components: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(registry_or_components, Mapping):
        raw = registry_or_components.get("components", [])
    else:
        raw = registry_or_components
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ComplianceDataError("components must be an array")
    components = [copy.deepcopy(dict(component)) for component in raw]
    seen: set[str] = set()
    for component in components:
        component_id = component.get("id")
        if not isinstance(component_id, str) or not component_id:
            raise ComplianceDataError("Every component must have a stable id")
        if component_id in seen:
            raise ComplianceDataError(f"Duplicate component id: {component_id}")
        seen.add(component_id)
        component.setdefault("observations", [])
    return components


def _version_value(component: Mapping[str, Any]) -> str | None:
    version = component.get("version")
    if isinstance(version, Mapping):
        value = version.get("value")
        return str(value) if value not in (None, "") else None
    return str(version) if version not in (None, "") else None


def _usage_categories(
    component: Mapping[str, Any], target: str | None = None, *, active: bool = False
) -> set[str]:
    if active and "active_usages" in component:
        raw_active = component.get("active_usages", [])
        if isinstance(raw_active, Sequence) and not isinstance(raw_active, (str, bytes)):
            active_categories = {str(category) for category in raw_active}
            if not target:
                return active_categories
            declared_for_target = _usage_categories(component, target, active=False)
            return active_categories & declared_for_target
    categories: set[str] = set()
    for usage in component.get("usages", []):
        if not isinstance(usage, Mapping):
            continue
        usage_target = str(usage.get("target", "*"))
        if target and usage_target not in ("*", target):
            continue
        category = usage.get("category")
        if isinstance(category, str):
            categories.add(category)
    return categories


def _most_precise_numeric_version(values: set[str]) -> str | None:
    parsed: dict[str, tuple[int, ...]] = {}
    for value in values:
        if not re.fullmatch(r"\d+(?:\.\d+)*", value):
            return None
        parsed[value] = tuple(int(part) for part in value.split("."))
    if not parsed:
        return None
    most_precise = max(parsed, key=lambda value: len(parsed[value]))
    precise_parts = parsed[most_precise]
    if all(precise_parts[: len(parts)] == parts for parts in parsed.values()):
        return most_precise
    return None


def _effective_version(component: Mapping[str, Any]) -> tuple[str | None, str]:
    declared = _version_value(component)
    status = "unknown"
    kind = "unknown"
    if isinstance(component.get("version"), Mapping):
        status = str(component["version"].get("status", "unknown"))
        kind = str(component["version"].get("kind", "unknown"))
    observed = {
        str(item["version"])
        for item in component.get("observations", [])
        if item.get("version") not in (None, "")
    }
    if status == "constraint-only" or "constraint" in kind:
        if len(observed) == 1:
            return next(iter(observed)), "observed"
        if len(observed) > 1:
            compatible = _most_precise_numeric_version(observed)
            return (compatible, "observed") if compatible else (None, "conflict")
        return declared, "constraint-only"
    if declared and observed and observed != {declared}:
        return None, "conflict"
    if declared:
        return declared, status
    if len(observed) == 1:
        return next(iter(observed)), "observed"
    return None, "conflict" if len(observed) > 1 else status

def _effective_origin(component: Mapping[str, Any]) -> tuple[str | None, str]:
    origin = component.get("origin", {})
    declared = origin.get("url") if isinstance(origin, Mapping) else origin
    declared_status = (
        str(origin.get("status", "unknown")) if isinstance(origin, Mapping) else "unknown"
    )
    observed = {
        str(item["origin"])
        for item in component.get("observations", [])
        if item.get("origin") not in (None, "")
    }
    if declared and observed and observed != {str(declared)}:
        return None, "conflict"
    if len(observed) == 1:
        return next(iter(observed)), "observed"
    if len(observed) > 1:
        return None, "conflict"
    return (str(declared) if declared else None), declared_status


def _usage_statuses(
    component: Mapping[str, Any], target: str, category: str
) -> set[str]:
    return {
        str(usage.get("status", "unknown"))
        for usage in component.get("usages", [])
        if isinstance(usage, Mapping)
        and usage.get("target") in (None, "*", target)
        and usage.get("category") == category
    }
