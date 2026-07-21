#!/usr/bin/env python3
"""Normalize Gitee local-CI results into the GitHub Pages data contracts."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOURCE_BRANCH = "ci/push/jiwang-delivery-ci"
DEFAULT_PROFILE = "sophgo-cmodel"
DEFAULT_RESULTS_WEB_URL = (
    "https://gitee.com/likehupochuan/triton-anchor-local-ci-results"
)
RUN_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-")


@dataclass(frozen=True)
class Run:
    source_branch: str
    sha: str
    run_id: str
    path: Path
    summary: dict[str, str]

    @property
    def tested_at(self) -> str:
        match = RUN_ID_RE.match(self.run_id)
        if not match:
            return ""
        value = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-branch", default=DEFAULT_SOURCE_BRANCH)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--backend-name", default="Sophgo")
    parser.add_argument("--results-branch", default="local-ci-results")
    parser.add_argument("--results-web-url", default=DEFAULT_RESULTS_WEB_URL)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def parse_summary(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def safe_branch(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "default"


def discover_runs(results_dir: Path, source_branch: str) -> list[Run]:
    branch_dir = results_dir / "runs" / safe_branch(source_branch)
    runs: list[Run] = []
    if not branch_dir.is_dir():
        return runs
    for sha_dir in branch_dir.iterdir():
        if not sha_dir.is_dir():
            continue
        for run_dir in sha_dir.iterdir():
            if not run_dir.is_dir() or not RUN_ID_RE.match(run_dir.name):
                continue
            summary = parse_summary(run_dir / "delivery-summary.txt")
            runs.append(Run(source_branch, sha_dir.name, run_dir.name, run_dir, summary))
    return sorted(runs, key=lambda run: run.run_id)


def status_code(run: Run) -> int | None:
    value = run.summary.get("status")
    if value is None:
        result = read_json(run.path / "result.json")
        value = str(result.get("status")) if result and "status" in result else None
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def normalize_stage(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"pass", "passed", "success"}:
        return "success"
    if normalized in {"warn", "warning"}:
        return "warning"
    if normalized in {"fail", "failed", "failure", "error"}:
        return "failure"
    if normalized in {"disabled", "skipped"}:
        return "disabled"
    return "unknown"


def result_url(run: Run, web_url: str, results_branch: str) -> str:
    relative = run.path.relative_to(run.path.parents[3]).as_posix()
    quoted_branch = urllib.parse.quote(results_branch, safe="")
    quoted_path = urllib.parse.quote(relative, safe="/")
    return f"{web_url.rstrip('/')}/tree/{quoted_branch}/{quoted_path}"


def backend_document(
    runs: list[Run], backend_name: str, profile: str, web_url: str, results_branch: str
) -> dict[str, Any]:
    latest = runs[-1] if runs else None
    if latest is None:
        sophgo = {
            "id": profile,
            "name": backend_name,
            "profile": profile,
            "state": "unknown",
            "sha": "",
            "branch": "",
            "tested_at": "",
            "tests": {
                "delivery": "unknown",
                "compile_time": "unknown",
                "pass_profile": "unknown",
                "ir_serialization": "unknown",
            },
            "result_url": "",
        }
    else:
        code = status_code(latest)
        tests = {
            "delivery": "success" if code == 0 else "failure" if code is not None else "unknown",
            "compile_time": normalize_stage(latest.summary.get("compile_time_status", "")),
            "pass_profile": normalize_stage(latest.summary.get("pass_profile_status", "")),
            "ir_serialization": normalize_stage(
                latest.summary.get("ir_serialization_status", "")
            ),
        }
        if tests["delivery"] == "failure":
            overall = "failure"
        elif "warning" in tests.values():
            overall = "warning"
        elif tests["delivery"] == "success":
            overall = "success"
        else:
            overall = "unknown"
        sophgo = {
            "id": profile,
            "name": backend_name,
            "profile": profile,
            "state": overall,
            "sha": latest.sha,
            "branch": latest.source_branch,
            "tested_at": latest.tested_at,
            "tests": tests,
            "result_url": result_url(latest, web_url, results_branch),
        }

    placeholders = []
    for suffix in ("b", "c", "d"):
        placeholders.append(
            {
                "id": f"backend-{suffix}",
                "name": f"Backend {suffix.upper()}",
                "profile": "待接入",
                "state": "unknown",
                "sha": "",
                "branch": "",
                "tested_at": "",
                "tests": {
                    "delivery": "unknown",
                    "compile_time": "unknown",
                    "pass_profile": "unknown",
                    "ir_serialization": "unknown",
                },
                "result_url": "",
            }
        )
    return {
        "schema": "triton-anchor-backend-status-list/v1",
        "data_mode": "live",
        "backends": [sophgo, *placeholders],
    }


def latest_valid_run(runs: Iterable[Run], file_name: str) -> tuple[Run | None, dict[str, Any] | None]:
    for run in reversed(list(runs)):
        document = read_json(run.path / file_name)
        if document is not None:
            return run, document
    return None, None


def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def nested_number(document: dict[str, Any] | None, *keys: str) -> float | None:
    value: object = document
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return number(value)


def status_for_delta(delta_percent: float | None, threshold_ratio: float, valid: bool = True) -> str:
    if not valid:
        return "failure"
    if delta_percent is not None and abs(delta_percent) > threshold_ratio * 100:
        return "warning"
    return "success"


def previous_document(runs: list[Run], current: Run | None, file_name: str) -> dict[str, Any] | None:
    if current is None:
        return None
    older = [run for run in runs if run.run_id < current.run_id and run.sha != current.sha]
    _, document = latest_valid_run(older, file_name)
    return document


def compile_rows(runs: list[Run], threshold_ratio: float) -> tuple[list[dict[str, Any]], Run | None]:
    run, candidate = latest_valid_run(runs, "compile-benchmark.json")
    if run is None or candidate is None:
        return [], None
    baseline = read_json(run.path / "compile-benchmark-base.json")
    if baseline is None:
        baseline = previous_document(runs, run, "compile-benchmark.json")
    metadata = candidate.get("metadata", {})
    kernels = metadata.get("kernels", []) if isinstance(metadata, dict) else []
    if not isinstance(kernels, list):
        kernels = []
    rows: list[dict[str, Any]] = []
    for kernel_value in kernels:
        kernel = str(kernel_value)
        candidate_ms = nested_number(candidate, "summary", kernel, "compile_est", "median_ms")
        if candidate_ms is None:
            continue
        baseline_ms = nested_number(baseline, "summary", kernel, "compile_est", "median_ms")
        delta = None
        if baseline_ms not in (None, 0):
            delta = ((candidate_ms / baseline_ms) - 1.0) * 100.0
        correct = bool(
            candidate.get("summary", {}).get(kernel, {}).get("all_correct", True)
            if isinstance(candidate.get("summary"), dict)
            else True
        )
        rows.append(
            {
                "name": kernel,
                "baseline_ms": baseline_ms,
                "candidate_ms": candidate_ms,
                "delta_percent": delta,
                "status": status_for_delta(delta, threshold_ratio, correct),
            }
        )
    return rows, run


def pass_rows(runs: list[Run], threshold_ratio: float) -> tuple[list[dict[str, Any]], Run | None]:
    run, candidate = latest_valid_run(runs, "pass-profile.json")
    if run is None or candidate is None:
        return [], None
    baseline = read_json(run.path / "pass-profile-base.json")
    if baseline is None:
        baseline = previous_document(runs, run, "pass-profile.json")
    rows: list[dict[str, Any]] = []
    summary = candidate.get("summary", {})
    if not isinstance(summary, dict):
        return rows, run
    for kernel, kernel_data in summary.items():
        if not isinstance(kernel_data, dict):
            continue
        hotspots = kernel_data.get("hotspots", [])
        if not isinstance(hotspots, list):
            continue
        for hotspot in hotspots:
            if not isinstance(hotspot, dict):
                continue
            pass_name = str(hotspot.get("name") or "")
            if not pass_name or pass_name in {"Total", "Rest"} or pass_name.startswith("(A)"):
                continue
            candidate_ms = number(hotspot.get("median_ms"))
            if candidate_ms is None:
                continue
            baseline_ms = nested_number(
                baseline, "summary", str(kernel), "passes", pass_name, "wall_ms", "median_ms"
            )
            delta = None
            if baseline_ms not in (None, 0):
                delta = ((candidate_ms / baseline_ms) - 1.0) * 100.0
            rows.append(
                {
                    "name": f"{kernel} / {pass_name}",
                    "median_ms": candidate_ms,
                    "delta_percent": delta,
                    "status": status_for_delta(delta, threshold_ratio),
                }
            )
    rows.sort(key=lambda row: float(row["median_ms"]), reverse=True)
    return rows[:10], run


def ir_rows(runs: list[Run], threshold_ratio: float) -> tuple[list[dict[str, Any]], Run | None]:
    run, candidate = latest_valid_run(runs, "ir-serialization.json")
    if run is None or candidate is None:
        return [], None
    baseline = read_json(run.path / "ir-serialization-base.json")
    if baseline is None:
        baseline = previous_document(runs, run, "ir-serialization.json")
    summary = candidate.get("summary", {})
    if not isinstance(summary, dict):
        return [], run
    metric_names = ("serialize", "write_text", "read_text", "deserialize", "roundtrip")
    rows: list[dict[str, Any]] = []
    for metric in metric_names:
        candidate_values = [
            nested_number(candidate, "summary", str(kernel), "metrics", metric, "median_ms")
            for kernel in summary
        ]
        candidate_values = [value for value in candidate_values if value is not None]
        if not candidate_values:
            continue
        candidate_ms = statistics.median(candidate_values)
        baseline_values = [
            nested_number(baseline, "summary", str(kernel), "metrics", metric, "median_ms")
            for kernel in summary
        ]
        baseline_values = [value for value in baseline_values if value is not None]
        baseline_ms = statistics.median(baseline_values) if baseline_values else None
        delta = None
        if baseline_ms not in (None, 0):
            delta = ((candidate_ms / baseline_ms) - 1.0) * 100.0
        rows.append(
            {
                "name": metric,
                "median_ms": candidate_ms,
                "baseline_ms": baseline_ms,
                "delta_percent": delta,
                "status": status_for_delta(delta, threshold_ratio),
            }
        )
    return rows, run


def performance_document(
    runs: list[Run], backend_name: str, profile: str, web_url: str, results_branch: str
) -> dict[str, Any]:
    threshold_ratio = 0.20
    compile_time, compile_run = compile_rows(runs, threshold_ratio)
    pass_profile, pass_run = pass_rows(runs, threshold_ratio)
    ir_serialization, ir_run = ir_rows(runs, threshold_ratio)
    source_runs = [run for run in (compile_run, pass_run, ir_run) if run is not None]
    newest = max(source_runs, key=lambda run: run.run_id) if source_runs else None

    def source(run: Run | None) -> dict[str, str] | None:
        if run is None:
            return None
        return {
            "sha": run.sha,
            "run_id": run.run_id,
            "result_url": result_url(run, web_url, results_branch),
        }

    return {
        "schema": "triton-anchor-performance-summary/v1",
        "data_mode": "live",
        "backend": f"{backend_name} CModel",
        "profile": profile,
        "sha": newest.sha if newest else "",
        "generated_at": newest.tested_at if newest else "",
        "sources": {
            "compile_time": source(compile_run),
            "pass_profile": source(pass_run),
            "ir_serialization": source(ir_run),
        },
        "compile_time": {
            "unit": "ms",
            "threshold": threshold_ratio,
            "kernels": compile_time,
        },
        "pass_profile": {"unit": "ms", "hotspots": pass_profile},
        "ir_serialization": {"unit": "ms", "metrics": ir_serialization},
    }


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sync_dashboard(
    results_dir: Path,
    output_dir: Path,
    source_branch: str = DEFAULT_SOURCE_BRANCH,
    profile: str = DEFAULT_PROFILE,
    backend_name: str = "Sophgo",
    results_branch: str = "local-ci-results",
    results_web_url: str = DEFAULT_RESULTS_WEB_URL,
) -> None:
    runs = discover_runs(results_dir, source_branch)
    if not runs:
        raise RuntimeError(f"No Gitee CI runs found for {source_branch!r}")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        output_dir / "backend-status.json",
        backend_document(runs, backend_name, profile, results_web_url, results_branch),
    )
    write_json(
        output_dir / "performance.json",
        performance_document(runs, backend_name, profile, results_web_url, results_branch),
    )

    manifest_path = output_dir / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None:
        raise RuntimeError(f"Dashboard manifest is missing or invalid: {manifest_path}")
    manifest["mode"] = "mixed"
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    manifest["data_modes"] = {
        "full_test": "mock",
        "backend_status": "live",
        "performance": "live",
    }
    write_json(manifest_path, manifest)


def main() -> int:
    args = parse_args()
    sync_dashboard(
        args.results_dir.resolve(),
        args.output_dir.resolve(),
        args.source_branch,
        args.profile,
        args.backend_name,
        args.results_branch,
        args.results_web_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
