"""Run OSV-Scanner against exact, candidate-observed package identities."""

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

from .discovery import normalize_build_evidence, reconcile_discoveries
from .model import (
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


def _query_inventory(
    registry: Mapping[str, Any],
    build_evidence: Mapping[str, Any],
    target: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build a scanner-only CycloneDX inventory from exact build observations."""

    validate_registry(registry)
    build_report = normalize_build_evidence(build_evidence)
    reconciliation = reconcile_discoveries(registry, [build_report])
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
        # This runner answers candidate/build coverage questions.  A declared
        # optional dependency is not part of that inventory until the supplied
        # build evidence actually observes it.
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
        version, version_status = _effective_version(component)
        purl = _sbom_purl(component)
        if (
            not version
            or version_status not in _EXACT_VERSION_STATUS
            or not purl
            or "@" not in purl.rsplit("/", 1)[-1]
        ):
            continue
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
        query_components.append(query_component)
        component_index[component_id] = {
            "id": component_id,
            "name": query_component["name"],
            "version": str(version),
            "base_purl": str(component["purl"]),
            "aliases": aliases,
            "expected_ecosystem": expected_ecosystem,
        }

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
    coverage_by_id: dict[str, dict[str, Any]] = {}
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
            if matched is None or not ecosystem:
                continue
            package["purl"] = matched["base_purl"]
            component_id = str(matched["id"])
            coverage_by_id[component_id] = {
                "component_id": component_id,
                "component_version": matched["version"],
                "status": "scanned",
                "scanner": "osv-scanner",
                "scanner_version": scanner_version,
                "scanned_on": scanned_on,
                "evidence": f"{raw_name}#sha256:{raw_sha256}",
            }

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
        query_sbom, component_index = _query_inventory(
            registry, build_evidence, target
        )
        if not component_index:
            raise OsvRunnerError("no exact PURL component is available for OSV query")
        scanner_version = _scanner_version(scanner_path)
        with tempfile.TemporaryDirectory(prefix="triton-anchor-osv-") as temporary:
            query_path = Path(temporary) / "query.cdx.json"
            query_path.write_text(stable_json(query_sbom), encoding="utf-8", newline="\n")
            try:
                result = subprocess.run(
                    [
                        str(scanner_path),
                        "scan",
                        "source",
                        "--sbom",
                        str(query_path),
                        "--format",
                        "json",
                        "--all-packages",
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
            raise OsvRunnerError("OSV-Scanner did not produce valid UTF-8 JSON") from exc
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--build-evidence", required=True)
    parser.add_argument("--raw-output", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", default="core-wheel")
    parser.add_argument("--scanned-on")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_json(args.registry)
        build_evidence = load_json(args.build_evidence)
    except (ComplianceDataError, OSError, ValueError) as exc:
        write_json(args.output, _failure_report(f"cannot read runner input: {exc}"))
        return 1
    return run_osv_scan(
        scanner=args.scanner,
        registry=registry,
        build_evidence=build_evidence,
        raw_output=args.raw_output,
        output=args.output,
        target=args.target,
        scanned_on=args.scanned_on,
    )


if __name__ == "__main__":
    raise SystemExit(main())
