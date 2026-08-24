"""Run OSV-Scanner against exact observed or dependency-admission identities."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .discovery import (
    _repository_identity,
    diff_components,
    normalize_build_evidence,
    reconcile_discoveries,
)
from .model import (
    AUDITED_USAGE,
    ComplianceDataError,
    _effective_version,
    _usage_categories,
    _usage_statuses,
    load_json,
    stable_json,
    validate_registry,
    write_json,
)
from .release import _EXACT_VERSION_STATUS, _sbom_purl
from .source_snapshot import source_snapshot_discoveries


_VERSION_PATTERN = re.compile(r"osv-scanner version:\s*([^\s]+)", re.IGNORECASE)
_PYPI_NAME_SEPARATOR = re.compile(r"[-_.]+")


class OsvRunnerError(RuntimeError):
    """Raised when OSV evidence cannot be produced without guessing coverage."""


def _component_id(component: Mapping[str, Any]) -> str:
    return str(component["id"])


def _purl_ecosystem(purl: str) -> str | None:
    package_type = purl.removeprefix("pkg:").split("/", 1)[0].casefold()
    return {"pypi": "PyPI"}.get(package_type)


def _package_name(value: Any, ecosystem: str | None) -> str:
    name = str(value or "").casefold()
    if ecosystem and ecosystem.casefold() == "pypi":
        return _PYPI_NAME_SEPARATOR.sub("-", name)
    return name


def _sbom_query_component(
    component: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    version, version_status = _effective_version(component)
    purl = _sbom_purl(component)
    if (
        not version
        or version_status not in _EXACT_VERSION_STATUS
        or not purl
        or "@" not in purl.rsplit("/", 1)[-1]
    ):
        return None
    component_id = _component_id(component)
    expected_ecosystem = _purl_ecosystem(str(purl))
    aliases = {
        _package_name(component_id, expected_ecosystem),
        _package_name(component.get("name", component_id), expected_ecosystem),
        *(
            _package_name(alias, expected_ecosystem)
            for alias in component.get("aliases", [])
        ),
    }
    query_component = {
        "bom-ref": f"osv-query:{component_id}@{version}",
        "type": component.get("type", component.get("kind", "library")),
        "name": component.get("name", component_id),
        "version": version,
        "purl": purl,
        "properties": [
            {"name": "triton-anchor:component-id", "value": component_id}
        ],
    }
    component_identity = {
        "id": component_id,
        "name": query_component["name"],
        "version": str(version),
        "base_purl": str(component["purl"]),
        "aliases": aliases,
        "expected_ecosystem": expected_ecosystem,
    }
    return query_component, component_identity


def _query_inventory(
    registry: Mapping[str, Any],
    build_evidence: Mapping[str, Any],
    target: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build a scanner-only CycloneDX inventory from exact build observations."""

    validate_registry(registry)
    build_report = normalize_build_evidence(build_evidence)
    reconciliation = reconcile_discoveries(
        registry, [build_report], target=target
    )
    if reconciliation["execution_issues"] or reconciliation["unmapped"]:
        reasons = [
            str(issue.get("reason", issue))
            for issue in reconciliation["execution_issues"]
        ]
        reasons.extend(
            f"unmapped build component: {item.get('component_id') or item.get('name')}"
            for item in reconciliation["unmapped"]
        )
        raise OsvRunnerError("; ".join(reasons) or "build evidence reconciliation failed")

    query_components: list[dict[str, Any]] = []
    component_index: dict[str, dict[str, Any]] = {}
    for component in reconciliation["components"]:
        if not component.get("third_party", True):
            continue
        active_usages = _usage_categories(component, target, active=True)
        if not active_usages or active_usages <= {"CI-only"}:
            continue
        if not any(
            isinstance(observation, Mapping)
            and observation.get("source") == "build-evidence"
            and observation.get("kind") == "build-component"
            for observation in component.get("observations", [])
        ):
            continue
        if all(
            _usage_statuses(component, target, category) == {"confirmed-optional"}
            for category in active_usages
        ):
            continue
        prepared = _sbom_query_component(component)
        if prepared is None:
            continue
        query_component, component_identity = prepared
        component_id = str(component_identity["id"])
        query_components.append(query_component)
        component_index[component_id] = component_identity

    query_components.sort(key=lambda item: str(item["bom-ref"]))
    return (
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.7",
            "version": 1,
            "components": query_components,
        },
        component_index,
    )


def _query_admission_inventory(
    baseline_registry: Mapping[str, Any],
    current_registry: Mapping[str, Any],
    target: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build exact package/commit queries for reviewed dependency changes."""

    validate_registry(baseline_registry)
    validate_registry(current_registry)
    difference = diff_components(baseline_registry, current_registry)
    changed_ids = {
        str(component["id"]) for component in difference["added"]
    } | {str(change["id"]) for change in difference["updated"]}
    current_by_id = {
        str(component["id"]): component
        for component in current_registry.get("components", [])
    }

    packages: list[dict[str, Any]] = []
    component_index: dict[str, dict[str, Any]] = {}
    for component_id in sorted(changed_ids):
        component = current_by_id[component_id]
        if not component.get("third_party", True):
            continue
        if not _usage_categories(component, target, active=True):
            continue
        version, version_status = _effective_version(component)
        if not version or version_status not in _EXACT_VERSION_STATUS:
            continue
        version_kind = component.get("version", {}).get("kind")
        origin = component.get("origin", {})
        repository_name = _repository_identity(
            origin.get("url") if isinstance(origin, Mapping) else origin
        )
        if (
            version_kind == "git-commit"
            and repository_name
            and re.fullmatch(r"[0-9a-fA-F]{40,64}", str(version))
        ):
            packages.append(
                {"package": {"name": repository_name, "commit": str(version)}}
            )
            component_index[component_id] = {
                "id": component_id,
                "name": component.get("name", component_id),
                "version": str(version),
                "commit": str(version).casefold(),
                "repository_name": repository_name,
                "base_purl": component.get("purl"),
                "aliases": set(),
                "expected_ecosystem": None,
            }
            continue

        base_purl = component.get("purl")
        expected_ecosystem = _purl_ecosystem(str(base_purl)) if base_purl else None
        if not base_purl or not expected_ecosystem:
            continue
        package_name = str(base_purl).rsplit("/", 1)[-1].split("@", 1)[0]
        aliases = {
            _package_name(component_id, expected_ecosystem),
            _package_name(component.get("name", component_id), expected_ecosystem),
            _package_name(package_name, expected_ecosystem),
            *(
                _package_name(alias, expected_ecosystem)
                for alias in component.get("aliases", [])
            ),
        }
        packages.append(
            {
                "package": {
                    "name": package_name,
                    "version": str(version),
                    "ecosystem": expected_ecosystem,
                }
            }
        )
        component_index[component_id] = {
            "id": component_id,
            "name": component.get("name", component_id),
            "version": str(version),
            "base_purl": str(base_purl),
            "aliases": aliases,
            "expected_ecosystem": expected_ecosystem,
        }

    packages.sort(
        key=lambda item: (
            str(item["package"].get("name", "")),
            str(item["package"].get("version", "")),
            str(item["package"].get("commit", "")),
        )
    )
    return {"results": [{"packages": packages}]}, component_index


def _query_source_inventory(
    registry: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
    dependency_inventory: Mapping[str, Any],
    target: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build exact commit queries for components present in a source snapshot."""

    validate_registry(registry)
    reconciliation = reconcile_discoveries(
        registry,
        [source_snapshot_discoveries(source_artifact), dependency_inventory],
        target=target,
    )
    if reconciliation["execution_issues"] or reconciliation["unmapped"]:
        reasons = [
            str(issue.get("reason", issue))
            for issue in reconciliation["execution_issues"]
        ]
        reasons.extend(
            f"unmapped source discovery: {item.get('name') or item.get('path')}"
            for item in reconciliation["unmapped"]
        )
        raise OsvRunnerError(
            "; ".join(reasons) or "source snapshot reconciliation failed"
        )

    packages: list[dict[str, Any]] = []
    component_index: dict[str, dict[str, Any]] = {}
    for component in reconciliation["components"]:
        if not component.get("third_party", True):
            continue
        active_usages = _usage_categories(component, target, active=True)
        if not active_usages & AUDITED_USAGE:
            continue
        version, version_status = _effective_version(component)
        version_kind = component.get("version", {}).get("kind")
        origin = component.get("origin", {})
        repository_name = _repository_identity(
            origin.get("url") if isinstance(origin, Mapping) else origin
        )
        if (
            version_kind != "git-commit"
            or version_status not in _EXACT_VERSION_STATUS
            or not version
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", version)
            or not repository_name
        ):
            continue
        component_id = _component_id(component)
        packages.append(
            {"package": {"name": repository_name, "commit": str(version)}}
        )
        component_index[component_id] = {
            "id": component_id,
            "name": component.get("name", component_id),
            "version": str(version),
            "commit": str(version).casefold(),
            "repository_name": repository_name,
            "base_purl": component.get("purl"),
            "aliases": set(),
            "expected_ecosystem": None,
        }
    packages.sort(
        key=lambda item: (
            str(item["package"]["name"]),
            str(item["package"]["commit"]),
        )
    )
    return {"results": [{"packages": packages}]}, component_index


def _scanner_version(scanner: Path) -> str:
    try:
        result = subprocess.run(
            [str(scanner), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OsvRunnerError(f"cannot execute OSV-Scanner: {exc}") from exc
    match = _VERSION_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or not match:
        raise OsvRunnerError("cannot determine OSV-Scanner version")
    return match.group(1)


def _unique_component_for_package(
    package: Mapping[str, Any], component_index: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    commit = package.get("commit")
    repository_name = _repository_identity(package.get("name"))
    if commit not in (None, "") and repository_name:
        matches = [
            component
            for component in component_index.values()
            if component.get("commit") == str(commit).casefold()
            and component.get("repository_name") == repository_name
        ]
        return matches[0] if len(matches) == 1 else None
    ecosystem = str(package.get("ecosystem", "")).strip()
    version = package.get("version")
    if not ecosystem or version in (None, ""):
        return None
    matches = [
        component
        for component in component_index.values()
        if component.get("expected_ecosystem")
        and ecosystem.casefold()
        == str(component["expected_ecosystem"]).casefold()
        and _package_name(package.get("name"), ecosystem) in component["aliases"]
        and str(version) == component["version"]
    ]
    return matches[0] if len(matches) == 1 else None


def _vulnerability_group_count(report: Mapping[str, Any]) -> int:
    results = report.get("results")
    if not isinstance(results, list):
        raise OsvRunnerError("OSV-Scanner JSON does not contain a results array")
    count = 0
    for result in results:
        if not isinstance(result, Mapping) or not isinstance(result.get("packages"), list):
            raise OsvRunnerError("OSV-Scanner result has an invalid packages array")
        for package_entry in result["packages"]:
            if not isinstance(package_entry, Mapping):
                raise OsvRunnerError("OSV-Scanner package entry is not an object")
            if not isinstance(package_entry.get("package"), Mapping):
                raise OsvRunnerError("OSV-Scanner package identity is not an object")
            groups = package_entry.get("groups", [])
            if not isinstance(groups, list):
                raise OsvRunnerError("OSV-Scanner package groups is not an array")
            for group in groups:
                if not isinstance(group, Mapping):
                    raise OsvRunnerError("OSV-Scanner vulnerability group is not an object")
                identifiers = group.get("ids") or group.get("aliases")
                if (
                    not isinstance(identifiers, Sequence)
                    or isinstance(identifiers, (str, bytes))
                    or not identifiers
                ):
                    raise OsvRunnerError(
                        "OSV-Scanner vulnerability group has no identifier"
                    )
                count += 1
    return count


def _enrich_report(
    report: Mapping[str, Any],
    component_index: Mapping[str, Mapping[str, Any]],
    *,
    scanner_version: str,
    scanned_on: str,
    raw_name: str,
    raw_sha256: str,
    scanner_exit_code: int,
) -> dict[str, Any]:
    results = report.get("results")
    if not isinstance(results, list):
        raise OsvRunnerError("OSV-Scanner JSON does not contain a results array")

    enriched = copy.deepcopy(dict(report))
    coverage_by_id: dict[str, dict[str, Any]] = {
        str(component["id"]): {
            "component_id": str(component["id"]),
            "component_version": component["version"],
            "status": "scanned",
            "scanner": "osv-scanner",
            "scanner_version": scanner_version,
            "scanned_on": scanned_on,
            "evidence": f"{raw_name}#sha256:{raw_sha256}",
        }
        for component in component_index.values()
    }
    for result in enriched["results"]:
        if not isinstance(result, Mapping):
            continue
        packages = result.get("packages", [])
        if not isinstance(packages, list):
            continue
        for package_entry in packages:
            if not isinstance(package_entry, dict):
                continue
            package = package_entry.get("package")
            if not isinstance(package, dict):
                continue
            matched = _unique_component_for_package(package, component_index)
            ecosystem = str(package.get("ecosystem", "")).strip()
            if matched is None:
                raise OsvRunnerError(
                    f"OSV-Scanner returned a package outside the exact query: {package.get('name')}"
                )
            if ecosystem and matched.get("base_purl"):
                package["purl"] = matched["base_purl"]
            component_id = str(matched["id"])
            package["component_id"] = component_id
            package.setdefault("version", matched["version"])
    enriched["coverage"] = [coverage_by_id[key] for key in sorted(coverage_by_id)]
    enriched["scanner_execution"] = {
        "status": "success",
        "scanner": "osv-scanner",
        "scanner_version": scanner_version,
        "scanned_on": scanned_on,
        "exit_code": scanner_exit_code,
        "query_component_count": len(component_index),
        "raw_report": raw_name,
        "raw_sha256": raw_sha256,
    }
    return enriched


def _failure_report(reason: str, scanner_version: str | None = None) -> dict[str, Any]:
    return {
        "results": None,
        "coverage": [],
        "scanner_execution": {
            "status": "failed",
            "scanner": "osv-scanner",
            "scanner_version": scanner_version,
            "reason": reason,
        },
    }


def _run_osv_query(
    *,
    scanner: str | Path,
    query_document: Mapping[str, Any],
    query_kind: str,
    component_index: Mapping[str, Mapping[str, Any]],
    raw_output: str | Path,
    output: str | Path,
    scanned_on: str | None,
) -> int:
    scanner_path = Path(scanner)
    raw_path = Path(raw_output)
    output_path = Path(output)
    if raw_path.resolve() == output_path.resolve():
        raise OsvRunnerError("raw and enriched OSV outputs must be different files")
    scan_date = scanned_on or datetime.now(timezone.utc).date().isoformat()
    try:
        date.fromisoformat(scan_date)
    except (TypeError, ValueError) as exc:
        raise OsvRunnerError("scanned_on must be an ISO date") from exc

    scanner_version: str | None = None
    try:
        if not component_index:
            raise OsvRunnerError("no exact component is available for OSV query")
        scanner_version = _scanner_version(scanner_path)
        with tempfile.TemporaryDirectory(prefix="triton-anchor-osv-") as temporary:
            query_name = (
                "query.cdx.json" if query_kind == "sbom" else "osv-scanner.json"
            )
            query_path = Path(temporary) / query_name
            query_path.write_text(
                stable_json(query_document), encoding="utf-8", newline="\n"
            )
            query_arguments = (
                ["--sbom", str(query_path), "--all-packages"]
                if query_kind == "sbom"
                else ["--lockfile", f"osv-scanner:{query_path}"]
            )
            try:
                result = subprocess.run(
                    [
                        str(scanner_path),
                        "scan",
                        "source",
                        *query_arguments,
                        "--format",
                        "json",
                        "--verbosity",
                        "error",
                    ],
                    check=False,
                    capture_output=True,
                )
            except OSError as exc:
                raise OsvRunnerError(f"cannot execute OSV-Scanner: {exc}") from exc

        raw_bytes = bytes(result.stdout)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw_bytes)
        if result.returncode not in (0, 1):
            raise OsvRunnerError(
                f"OSV-Scanner failed with exit code {result.returncode}"
            )
        try:
            raw_report = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OsvRunnerError(
                "OSV-Scanner did not produce valid UTF-8 JSON"
            ) from exc
        if not isinstance(raw_report, Mapping):
            raise OsvRunnerError("OSV-Scanner JSON root must be an object")
        finding_count = _vulnerability_group_count(raw_report)
        if result.returncode == 0 and finding_count:
            raise OsvRunnerError(
                "OSV-Scanner exit code 0 conflicts with vulnerability findings"
            )
        if result.returncode == 1 and not finding_count:
            raise OsvRunnerError(
                "OSV-Scanner exit code 1 has no vulnerability finding"
            )
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        enriched = _enrich_report(
            raw_report,
            component_index,
            scanner_version=scanner_version,
            scanned_on=scan_date,
            raw_name=raw_path.name,
            raw_sha256=raw_sha256,
            scanner_exit_code=result.returncode,
        )
        write_json(output_path, enriched)
        return 0
    except (ComplianceDataError, OsvRunnerError, OSError, ValueError) as exc:
        write_json(output_path, _failure_report(str(exc), scanner_version))
        return 1


def run_osv_scan(
    *,
    scanner: str | Path,
    registry: Mapping[str, Any],
    build_evidence: Mapping[str, Any],
    raw_output: str | Path,
    output: str | Path,
    target: str = "core-wheel",
    scanned_on: str | None = None,
) -> int:
    """Run OSV-Scanner and write immutable raw plus enriched gate input."""

    try:
        query_sbom, component_index = _query_inventory(
            registry, build_evidence, target
        )
    except (ComplianceDataError, OsvRunnerError, OSError, ValueError) as exc:
        write_json(output, _failure_report(str(exc)))
        return 1
    return _run_osv_query(
        scanner=scanner,
        query_document=query_sbom,
        query_kind="sbom",
        component_index=component_index,
        raw_output=raw_output,
        output=output,
        scanned_on=scanned_on,
    )


def run_osv_admission_scan(
    *,
    scanner: str | Path,
    baseline_registry: Mapping[str, Any],
    current_registry: Mapping[str, Any],
    raw_output: str | Path,
    output: str | Path,
    target: str = "core-wheel",
    scanned_on: str | None = None,
) -> int:
    """Run exact OSV queries for components changed by a dependency PR."""

    try:
        query, component_index = _query_admission_inventory(
            baseline_registry, current_registry, target
        )
    except (ComplianceDataError, OsvRunnerError, OSError, ValueError) as exc:
        write_json(output, _failure_report(str(exc)))
        return 1
    if not component_index:
        write_json(
            output,
            {
                "results": [],
                "coverage": [],
                "scanner_execution": {
                    "status": "not-applicable",
                    "scanner": "osv-scanner",
                    "scanner_version": None,
                    "scanned_on": scanned_on
                    or datetime.now(timezone.utc).date().isoformat(),
                    "query_component_count": 0,
                    "reason": "no exact changed component is active for this target",
                },
            },
        )
        return 0
    return _run_osv_query(
        scanner=scanner,
        query_document=query,
        query_kind="custom-lockfile",
        component_index=component_index,
        raw_output=raw_output,
        output=output,
        scanned_on=scanned_on,
    )


def run_osv_source_scan(
    *,
    scanner: str | Path,
    registry: Mapping[str, Any],
    source_artifact: Mapping[str, Any],
    dependency_inventory: Mapping[str, Any],
    raw_output: str | Path,
    output: str | Path,
    target: str = "source-snapshot",
    scanned_on: str | None = None,
) -> int:
    """Run exact Git commit queries for a verified source snapshot inventory."""

    try:
        query, component_index = _query_source_inventory(
            registry, source_artifact, dependency_inventory, target
        )
    except (ComplianceDataError, OsvRunnerError, OSError, ValueError) as exc:
        write_json(output, _failure_report(str(exc)))
        return 1
    return _run_osv_query(
        scanner=scanner,
        query_document=query,
        query_kind="custom-lockfile",
        component_index=component_index,
        raw_output=raw_output,
        output=output,
        scanned_on=scanned_on,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner", required=True)
    parser.add_argument("--registry", required=True)
    inventory = parser.add_mutually_exclusive_group(required=True)
    inventory.add_argument("--build-evidence")
    inventory.add_argument("--source-artifact")
    inventory.add_argument("--baseline-registry")
    parser.add_argument("--dependency-inventory")
    parser.add_argument("--raw-output", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target")
    parser.add_argument("--scanned-on")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_json(args.registry)
        if args.baseline_registry:
            baseline_registry = load_json(args.baseline_registry)
        elif args.source_artifact:
            if not args.dependency_inventory:
                raise ComplianceDataError(
                    "--dependency-inventory is required with --source-artifact"
                )
            source_artifact = load_json(args.source_artifact)
            dependency_inventory = load_json(args.dependency_inventory)
        else:
            build_evidence = load_json(args.build_evidence)
    except (ComplianceDataError, OSError, ValueError) as exc:
        write_json(args.output, _failure_report(f"cannot read runner input: {exc}"))
        return 1
    if args.baseline_registry:
        return run_osv_admission_scan(
            scanner=args.scanner,
            baseline_registry=baseline_registry,
            current_registry=registry,
            raw_output=args.raw_output,
            output=args.output,
            target=args.target or "core-wheel",
            scanned_on=args.scanned_on,
        )
    if args.source_artifact:
        return run_osv_source_scan(
            scanner=args.scanner,
            registry=registry,
            source_artifact=source_artifact,
            dependency_inventory=dependency_inventory,
            raw_output=args.raw_output,
            output=args.output,
            target=args.target or "source-snapshot",
            scanned_on=args.scanned_on,
        )
    return run_osv_scan(
        scanner=args.scanner,
        registry=registry,
        build_evidence=build_evidence,
        raw_output=args.raw_output,
        output=args.output,
        target=args.target or "core-wheel",
        scanned_on=args.scanned_on,
    )


if __name__ == "__main__":
    raise SystemExit(main())
