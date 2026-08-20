"""Evidence normalization, discovery matching, and inventory diffing."""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from .model import BUILD_EVIDENCE_BINDINGS, _component_list, _version_value


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

def _component_patterns(
    component: Mapping[str, Any], kind: str, path_scope: str = "source"
) -> list[str]:
    patterns: list[str] = []
    usage_key = {
        "artifact-file": "artifact_patterns",
        "dependency-declaration": "declaration_patterns",
    }.get(kind, "path_patterns")
    if path_scope == "artifact" and kind in {"license", "package", "source-file"}:
        usage_key = "artifact_patterns"
    for usage in component.get("usages", []):
        if isinstance(usage, Mapping):
            raw = usage.get(usage_key, [])
            if isinstance(raw, str):
                patterns.append(raw)
            elif isinstance(raw, Sequence):
                patterns.extend(str(item) for item in raw)
    evidence_kinds = {
        "artifact-file": {"artifact-path"},
        "license": {"source-path", "license-file"},
        "package": {"source-path", "package-file"},
        "source-file": {"source-path"},
    }.get(kind, {"source-path"})
    for evidence in component.get("evidence", []):
        if not isinstance(evidence, Mapping) or evidence.get("kind") not in evidence_kinds:
            continue
        location = evidence.get("location")
        if isinstance(location, str) and location:
            patterns.append(location)
    return sorted(set(patterns))


def _usage_is_active(component: Mapping[str, Any], usage: Mapping[str, Any]) -> bool:
    category = str(usage.get("category", ""))
    status = str(usage.get("status", ""))
    observations = component.get("observations", [])
    if category == "distributed":
        patterns = usage.get("artifact_patterns", [])
        return any(
            observation.get("kind") == "artifact-file"
            and isinstance(observation.get("path"), str)
            and any(
                _path_matches(str(pattern), str(observation["path"]))
                for pattern in patterns
            )
            for observation in observations
        )
    if category in {
        "embedded",
        "runtime-external",
        "build-only",
        "test-only",
        "CI-only",
    }:
        if category == "runtime-external" and status == "confirmed-optional":
            return True
        return any(
            category in observation.get("candidate_usages", [])
            for observation in observations
        )
    return False


def _path_matches(pattern: str, path: str) -> bool:
    normalized_pattern = PurePosixPath(pattern.replace("\\", "/")).as_posix()
    normalized_path = PurePosixPath(path.replace("\\", "/")).as_posix()
    return fnmatch.fnmatchcase(normalized_path, normalized_pattern)


def _purl_identity(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("pkg:"):
        return None
    package = value.split("#", 1)[0].split("?", 1)[0]
    version_marker = package.rfind("@")
    if version_marker > package.rfind("/"):
        package = package[:version_marker]
    return package.casefold()


def _identity_matches(component: Mapping[str, Any], discovery: Mapping[str, Any]) -> bool:
    component_purl = _purl_identity(component.get("purl"))
    discovery_purl = _purl_identity(discovery.get("purl"))
    if component_purl and discovery_purl == component_purl:
        return True
    aliases = {str(alias).casefold() for alias in component.get("aliases", [])}
    aliases.add(str(component.get("name", "")).casefold())
    return bool(discovery.get("name") and str(discovery["name"]).casefold() in aliases)


def _match_discovery(
    components: Sequence[Mapping[str, Any]], discovery: Mapping[str, Any]
) -> list[str]:
    explicit = discovery.get("component_id")
    if explicit:
        return [str(explicit)] if any(component["id"] == explicit for component in components) else []

    identity_matches = [component["id"] for component in components if _identity_matches(component, discovery)]
    if len(identity_matches) == 1:
        return identity_matches

    path = discovery.get("path")
    if not isinstance(path, str) or not path:
        return []
    matches: list[tuple[int, str]] = []
    for component in components:
        for pattern in _component_patterns(
            component,
            str(discovery.get("kind", "source-file")),
            str(discovery.get("path_scope", "source")),
        ):
            if _path_matches(pattern, path):
                specificity = len(pattern.replace("*", "").replace("?", ""))
                matches.append((specificity, str(component["id"])))
    if not matches:
        return []
    if discovery.get("allow_multiple"):
        return sorted({component_id for _, component_id in matches})
    best = max(score for score, _ in matches)
    best_ids = sorted({component_id for score, component_id in matches if score == best})
    return best_ids if len(best_ids) == 1 else []


def reconcile_discoveries(
    registry: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    discoveries: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, Any]:
    """Attach every machine finding to the canonical component model.

    Manual metadata can enrich a finding, but cannot make an unmapped machine
    finding disappear.  Explicit exclusions remain visible and require a reason.
    """

    components = _component_list(registry)
    by_id = {component["id"]: component for component in components}
    reports = [discoveries] if isinstance(discoveries, Mapping) else list(discoveries)
    execution_issues: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    mapped: list[dict[str, Any]] = []

    items: list[dict[str, Any]] = []
    # Scanner-name keyed input is accepted as a convenience for callers that
    # have already normalized each scanner's small summary.
    if len(reports) == 1 and isinstance(reports[0], Mapping) and "items" not in reports[0]:
        keyed = reports[0]
        if any(key in keyed for key in ("scancode", "wheel", "syft", "build-evidence")):
            reports = []
            for source, summary in keyed.items():
                if not isinstance(summary, Mapping):
                    continue
                items_for_source: list[dict[str, Any]] = []
                for finding in summary.get("license_files", []):
                    items_for_source.append(
                        {
                            "kind": "license",
                            "path": finding.get("path"),
                            "license_expression": finding.get("expression"),
                        }
                    )
                for native in summary.get("native_files", []):
                    items_for_source.append(
                        {
                            "kind": "artifact-file",
                            "path": native.get("path"),
                            "sha256": native.get("sha256"),
                            "allow_multiple": True,
                        }
                    )
                scan_errors = summary.get("scan_errors", [])
                reports.append(
                    {
                        "source": source,
                        "status": "success" if summary.get("status") in {"ok", "success"} else "failed",
                        "items": items_for_source,
                        "issues": [str(error) for error in scan_errors],
                    }
                )

    for report in reports:
        if "items" in report:
            source = str(report.get("source", "unknown"))
            report_issues = [str(issue) for issue in report.get("issues", [])]
            if report.get("status", "success") != "success" and not report_issues:
                execution_issues.append(
                    {"source": source, "reason": report.get("reason", "scanner did not succeed")}
                )
            for issue in report_issues:
                execution_issues.append({"source": source, "reason": issue})
            for item in report.get("items", []):
                copied = dict(item)
                copied.setdefault("source", source)
                items.append(copied)
        else:
            items.append(dict(report))

    for item in items:
        if item.get("excluded"):
            if not item.get("reason"):
                unmapped.append({**item, "mapping_reason": "exclusion has no reason"})
            else:
                excluded.append(item)
            continue
        component_ids = _match_discovery(components, item)
        if not component_ids:
            unmapped.append({**item, "mapping_reason": "no unique component match"})
            continue
        attached = {**item, "component_ids": component_ids}
        mapped.append(attached)
        for component_id in component_ids:
            by_id[component_id]["observations"].append(attached)

    for component in components:
        component["observations"] = sorted(
            component["observations"],
            key=lambda item: (
                str(item.get("source", "")),
                str(item.get("kind", "")),
                str(item.get("path", "")),
                str(item.get("id", "")),
            ),
        )
        component["active_usages"] = sorted(
            {
                str(usage["category"])
                for usage in component.get("usages", [])
                if isinstance(usage, Mapping) and _usage_is_active(component, usage)
            }
        )
    issues: list[dict[str, Any]] = [
        {"code": "scanner-failed", **issue} for issue in execution_issues
    ]
    issues.extend(
        {
            "code": "unmapped-distributed-file"
            if item.get("kind") == "artifact-file"
            else "unmapped-discovery",
            **item,
        }
        for item in unmapped
    )
    return {
        "components": components,
        "mapped": mapped,
        "unmapped": unmapped,
        "excluded": excluded,
        "execution_issues": execution_issues,
        "issues": issues,
    }


def _scancode_input_name(report: Mapping[str, Any]) -> str | None:
    headers = report.get("headers")
    if not isinstance(headers, list):
        return None
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        options = header.get("options")
        inputs = options.get("input") if isinstance(options, Mapping) else None
        if not isinstance(inputs, list) or len(inputs) != 1:
            continue
        raw = str(inputs[0]).replace("\\", "/").rstrip("/")
        name = PurePosixPath(raw).name
        return name or None
    return None


def _scancode_relative_path(path: Any, input_name: str | None) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    normalized = path.replace("\\", "/")
    if input_name and normalized.startswith(f"{input_name}/"):
        normalized = normalized[len(input_name) + 1 :]
    if (
        not normalized
        or normalized.startswith("/")
        or _WINDOWS_DRIVE.match(normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        return None
    return PurePosixPath(normalized).as_posix()


def normalize_scancode(
    report: Mapping[str, Any],
    source: str = "scancode-source",
    path_scope: str = "source",
) -> dict[str, Any]:
    if path_scope not in {"source", "artifact"}:
        raise ValueError("ScanCode path_scope must be source or artifact")
    items: list[dict[str, Any]] = []
    issues: list[str] = []
    if "license_files" in report and "status" in report:
        for finding in report.get("license_files", []):
            if not isinstance(finding, Mapping):
                continue
            path = _scancode_relative_path(finding.get("path"), None)
            if not path:
                issues.append(f"ScanCode path is not input-relative: {finding.get('path')!r}")
                continue
            items.append(
                {
                    "kind": "license",
                    "path": path,
                    "path_scope": path_scope,
                    "license_expression": finding.get("expression"),
                    "source": source,
                }
            )
        for package in report.get("packages", []):
            if not isinstance(package, Mapping):
                continue
            items.append(
                {
                    "kind": "package",
                    "name": package.get("name"),
                    "version": package.get("version"),
                    "purl": package.get("purl"),
                    "license_expression": package.get("declared_license"),
                    "source": source,
                }
            )
        for error in report.get("scan_errors", []):
            issues.append(str(error))
        succeeded = (
            report.get("status") == "ok"
            and report.get("returncode", 0) == 0
            and report.get("scan_error_count", len(issues)) == 0
        )
        return {
            "source": source,
            "status": "success" if succeeded and not issues else "failed",
            "items": items,
            "issues": issues,
        }
    headers = report.get("headers")
    if not isinstance(headers, list) or not headers:
        return {"source": source, "status": "failed", "items": [], "issues": ["missing ScanCode header"]}
    input_name = _scancode_input_name(report)
    file_entries = report.get("files")
    if not isinstance(file_entries, list) or not any(
        isinstance(entry, Mapping) and entry.get("type") == "file"
        for entry in file_entries
    ):
        issues.append("ScanCode report contains no scanned files")
    for header in headers:
        for error in header.get("errors", []) if isinstance(header, Mapping) else []:
            issues.append(str(error))
    for file_entry in file_entries if isinstance(file_entries, list) else []:
        if not isinstance(file_entry, Mapping):
            continue
        raw_path = file_entry.get("path")
        path = _scancode_relative_path(raw_path, input_name)
        if not path:
            # ScanCode includes one directory record for the scan root.  It is
            # not a file finding and carries no component evidence.
            if input_name and raw_path == input_name and file_entry.get("type") == "directory":
                continue
            issues.append(f"ScanCode path is not input-relative: {raw_path!r}")
            continue
        for error in file_entry.get("scan_errors", []) or []:
            issues.append(f"{path}: {error}")
        for detection in file_entry.get("license_detections", []) or []:
            if not isinstance(detection, Mapping):
                continue
            expression = detection.get("license_expression_spdx") or detection.get("license_expression")
            if expression:
                items.append(
                    {
                        "kind": "license",
                        "path": path,
                        "path_scope": path_scope,
                        "license_expression": str(expression),
                        "source": source,
                    }
                )
    for package in report.get("packages", []):
        if not isinstance(package, Mapping):
            continue
        expression = package.get("declared_license_expression_spdx") or package.get("declared_license_expression")
        package_paths = package.get("datafile_paths") or []
        item = {
            "kind": "package",
            "name": package.get("name"),
            "version": package.get("version"),
            "purl": package.get("purl"),
            "source": source,
        }
        if package_paths:
            package_path = _scancode_relative_path(package_paths[0], input_name)
            if package_path:
                item["path"] = package_path
                item["path_scope"] = path_scope
        if expression:
            item["license_expression"] = str(expression)
        items.append(item)
    return {
        "source": source,
        "status": "failed" if issues else "success",
        "items": items,
        "issues": issues,
    }


def normalize_cyclonedx(report: Mapping[str, Any], source: str = "syft") -> dict[str, Any]:
    if report.get("bomFormat") != "CycloneDX" or not isinstance(report.get("components"), list):
        return {"source": source, "status": "failed", "items": [], "issues": ["invalid CycloneDX report"]}
    items: list[dict[str, Any]] = []
    for component in report["components"]:
        if not isinstance(component, Mapping):
            continue
        if component.get("type") == "file":
            continue
        expression = None
        licenses = component.get("licenses") or []
        expressions = [entry.get("expression") for entry in licenses if isinstance(entry, Mapping) and entry.get("expression")]
        if len(expressions) == 1:
            expression = expressions[0]
        item = {
            "kind": "package",
            "name": component.get("name"),
            "version": component.get("version"),
            "purl": component.get("purl"),
            "source": source,
        }
        if expression:
            item["license_expression"] = expression
        items.append(item)
    issues = [] if items else ["CycloneDX report contains no package components"]
    return {
        "source": source,
        "status": "failed" if issues else "success",
        "items": items,
        "issues": issues,
    }


def normalize_build_evidence(
    report: Mapping[str, Any], expected_artifact_sha256: str | None = None
) -> dict[str, Any]:
    payload = report.get("compliance_build", report)
    if not isinstance(payload, Mapping):
        return {
            "source": "build-evidence",
            "status": "failed",
            "items": [],
            "issues": ["compliance_build must be an object"],
        }
    items: list[dict[str, Any]] = []
    issues: list[str] = []
    report_succeeded = payload.get("status") == "success"
    if not report_succeeded:
        issues.append(
            str(
                payload.get(
                    "reason",
                    "build evidence must explicitly declare status=success",
                )
            )
        )
    artifact_sha256 = payload.get("artifact_sha256")
    if (
        report_succeeded
        and expected_artifact_sha256
        and artifact_sha256 != expected_artifact_sha256
    ):
        issues.append("build evidence does not identify this assessed artifact")
    evidence_binding = str(payload.get("evidence_binding", "unknown"))
    if evidence_binding not in BUILD_EVIDENCE_BINDINGS:
        issues.append(
            "build evidence binding must be same-build, post-hoc, or unknown"
        )
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        issues.append("build evidence components must be a non-empty array")
        components = []
    native = payload.get("native")
    if isinstance(native, Mapping):
        unmapped_sonames = native.get("unmapped_sonames", [])
        if not isinstance(unmapped_sonames, list) or any(
            not isinstance(soname, str) or not soname for soname in unmapped_sonames
        ):
            issues.append("build evidence unmapped_sonames must be an array of names")
        elif unmapped_sonames:
            issues.append(
                "unmapped ELF dependencies: " + ", ".join(sorted(unmapped_sonames))
            )
    for component in components:
        if not isinstance(component, Mapping):
            issues.append("build evidence component entries must be objects")
            continue
        candidate_usages = component.get("usages", [])
        if not component.get("id") or "version" not in component:
            issues.append("build component is missing id or version field")
            continue
        if not isinstance(candidate_usages, list) or not candidate_usages:
            issues.append(
                f"build component {component.get('id')} does not declare its candidate usages"
            )
            continue
        invalid_usages = sorted(
            str(category)
            for category in candidate_usages
            if category not in {"embedded", "runtime-external", "build-only"}
        )
        if invalid_usages:
            issues.append(
                f"build component {component.get('id')} has invalid usages: {invalid_usages}"
            )
            continue
        item = {
            "kind": "build-component",
            "component_id": component["id"],
            "purl": component.get("purl"),
            "origin": component.get("origin"),
            "candidate_usages": sorted(set(str(item) for item in candidate_usages)),
            "source": "build-evidence",
            "evidence": component.get("evidence"),
        }
        if component.get("version") not in (None, ""):
            item["version"] = str(component["version"])
        items.append(item)
    return {
        "source": "build-evidence",
        "status": "failed" if issues else "success",
        "artifact_sha256": artifact_sha256,
        "evidence_binding": evidence_binding,
        "items": items,
        "issues": issues,
    }


def wheel_discoveries(artifact: Mapping[str, Any]) -> dict[str, Any]:
    items = [
        {
            "kind": "artifact-file",
            "path": member["path"],
            "size": member["size"],
            **({"sha256": member["sha256"]} if member.get("sha256") else {}),
            "native": bool(member.get("sha256")),
            "source": "wheel",
        }
        for member in artifact.get("files", [])
    ]
    python_tag = str(artifact.get("python_tag", ""))
    match = re.fullmatch(r"cp(\d)(\d+)", python_tag)
    if match:
        items.append(
            {
                "kind": "runtime-component",
                "component_id": "cpython",
                "version": f"{match.group(1)}.{match.group(2)}",
                "candidate_usages": ["runtime-external"],
                "source": "wheel",
            }
        )
    for dependency in artifact.get("python_imports", []):
        if not isinstance(dependency, Mapping) or not dependency.get("name"):
            continue
        paths = dependency.get("paths", [])
        test_only = dependency.get("context") == "test-only"
        items.append(
            {
                "kind": "dependency-declaration",
                "name": dependency["name"],
                "path": paths[0] if paths else None,
                "paths": list(paths),
                "candidate_usages": [
                    "test-only" if test_only else "runtime-external"
                ],
                **(
                    {
                        "excluded": True,
                        "reason": "import appears only in packaged test modules",
                    }
                    if test_only
                    else {}
                ),
                "source": "wheel",
            }
        )
    import_issues = [str(issue) for issue in artifact.get("python_import_issues", [])]
    return {
        "source": "wheel",
        "status": "failed" if import_issues else "success",
        "issues": import_issues,
        "items": items,
    }


def diff_components(
    baseline: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    current: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    base = {component["id"]: component for component in _component_list(baseline)}
    head = {component["id"]: component for component in _component_list(current)}
    added = [head[key] for key in sorted(head.keys() - base.keys())]
    removed = [base[key] for key in sorted(base.keys() - head.keys())]
    updated: list[dict[str, Any]] = []
    tracked_fields = ("name", "purl", "origin", "version", "license", "usages", "depends_on")
    for key in sorted(base.keys() & head.keys()):
        changed_fields = [
            field for field in tracked_fields if base[key].get(field) != head[key].get(field)
        ]
        if changed_fields:
            updated.append(
                {
                    "id": key,
                    "from_version": _version_value(base[key]),
                    "to_version": _version_value(head[key]),
                    "changed_fields": changed_fields,
                }
            )
    return {"added": added, "removed": removed, "updated": updated}

def normalize_osv(report: Mapping[str, Any], components: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(report.get("results"), list):
        return {"status": "failed", "vulnerabilities": [], "issues": ["invalid OSV report"]}
    canonical = _component_list(components)
    vulnerabilities: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    issues: list[str] = []
    for entry in report.get("coverage", []):
        if not isinstance(entry, Mapping):
            issues.append("OSV coverage entry is not an object")
            continue
        component_id = entry.get("component_id")
        status = entry.get("status")
        if not component_id or not status:
            issues.append("OSV coverage entry is missing component_id or status")
            continue
        coverage.append(dict(entry))
    for result in report["results"]:
        for package_entry in result.get("packages", []) if isinstance(result, Mapping) else []:
            package = package_entry.get("package", {})
            identity = {"name": package.get("name"), "purl": package.get("purl")}
            matched = [component["id"] for component in canonical if _identity_matches(component, identity)]
            component_id = matched[0] if len(matched) == 1 else None
            if component_id is None:
                issues.append(f"OSV package is not mapped: {package.get('name')}")
            for group in package_entry.get("groups", []):
                score = 0.0
                try:
                    score = float(group.get("max_severity", 0))
                except (TypeError, ValueError):
                    pass
                severity = "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"
                group_ids = group.get("ids") or group.get("aliases") or ["unknown"]
                vulnerabilities.append(
                    {
                        "id": str(group_ids[0]),
                        "aliases": sorted(set(str(value) for value in group_ids)),
                        "component_id": component_id,
                        "component_version": package.get("version"),
                        "severity": severity,
                        "status": "open",
                    }
                )
    return {
        "status": "failed" if issues else "success",
        "vulnerabilities": vulnerabilities,
        "coverage": coverage,
        "issues": issues,
    }
