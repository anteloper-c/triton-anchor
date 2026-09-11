"""Common report criteria for builtin plans and recorded native executions.

This module evaluates collected facts. It does not attest arbitrary same-UID
reports or infer coverage merely from a zero exit status.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .actions import validate_measurements


def junit(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    counts = {
        "tests": len(cases),
        "passed": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    observed = []
    for case in cases:
        outcome = next(
            (
                name
                for name, tag in (
                    ("errors", "error"),
                    ("failures", "failure"),
                    ("skipped", "skipped"),
                )
                if case.find(tag) is not None
            ),
            "passed",
        )
        counts[outcome] += 1
        observed.append(
            {
                "file": case.get("file", ""),
                "class": case.get("classname", ""),
                "name": case.get("name", ""),
                "status": outcome,
            }
        )
    return {
        **counts,
        "cases": observed,
        "junit_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def evaluate(
    tool_id: str,
    artifact_dir: str | Path,
    exit_code: int,
    context: dict,
    parameters: dict | None = None,
) -> dict:
    parameters = parameters or {}
    root = Path(artifact_dir).resolve()
    subject = {
        key: context[key]
        for key in (
            "task_id",
            "target_sha",
            "environment_fingerprint",
            "source_identity",
            "source_digest",
            "source_sha256",
            "task_venv",
        )
        if key in context
    }
    result = {
        "status": "pass",
        "details": {},
        "subject": subject,
        "scope": {"tool_id": tool_id},
        "evidence_files": [],
    }
    if exit_code:
        result.update(
            status="cancelled"
            if exit_code in (130, 143, -15, -2)
            else "infra_error"
            if exit_code in (78, 124, 126, 127)
            or tool_id in {"environment", "frontend_install", "backend_install"}
            else "fail",
            reason=f"Command exited with {exit_code}",
        )

    def report(name: str, default: str) -> Path:
        path = root / parameters.get("reports", {}).get(name, default)
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or not path.is_file()
        ):
            raise ValueError("Missing or invalid " + name + " report")
        relative = path.relative_to(root).as_posix()
        if relative not in result["evidence_files"]:
            result["evidence_files"].append(relative)
        return path

    def document(name: str, default: str) -> dict:
        value = json.loads(report(name, default).read_text())
        if not isinstance(value, dict):
            raise ValueError(name + " report must be an object")
        return value

    def identity(value: dict) -> None:
        for name in ("task_id", "target_sha"):
            if value.get(name) != context.get(name):
                raise ValueError("Report belongs to another " + name)
        if context.get("environment_fingerprint") and value.get(
            "environment_fingerprint"
        ) not in (None, context["environment_fingerprint"]):
            raise ValueError("Report belongs to another environment")

    def test_report() -> dict:
        value = junit(report("junit", "tests.xml"))
        if not value["passed"] or value["failures"] or value["errors"]:
            raise AssertionError(
                "JUnit must contain executed passing tests without failures or errors"
            )
        summary_path = root / parameters.get("reports", {}).get("tests", "tests.json")
        if summary_path.is_file():
            summary = document("tests", "tests.json")
            identity(summary)
            if summary.get("junit_sha256") != value["junit_sha256"]:
                raise ValueError(
                    "Test summary does not match the observed JUnit report"
                )
        result["scope"]["observed_tests"] = value["cases"]
        return value

    try:
        if tool_id == "environment":
            value = document("environment", "environment.json")
            identity(value)
            if value.get("missing") or not value.get("environment_fingerprint"):
                raise ValueError("Environment is incomplete or has no fingerprint")
            result["subject"]["environment_fingerprint"] = value[
                "environment_fingerprint"
            ]
            result["details"]["environment"] = value
        elif tool_id in {"frontend_build", "backend_build"}:
            value = document("wheel", "wheel.json")
            identity(value)
            wheel = report("wheel_file", "wheels/" + Path(value["wheel"]).name)
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != value.get("sha256"):
                raise ValueError("Wheel hash differs from the build report")
            result["details"]["wheel"] = value
            result["subject"]["wheel_sha256"] = value["sha256"]
        elif tool_id in {"frontend_install", "backend_install"}:
            value = document("installation", "installation.json")
            identity(value)
            if not value.get("sha256") or not value.get("python_executable"):
                raise ValueError("Installation has no wheel or Python identity")
            if (
                context.get("python_bin")
                and value["python_executable"] != context["python_bin"]
            ):
                raise ValueError("Installation uses a different task interpreter")
            if (
                context.get("task_venv")
                and value.get("task_venv") != context["task_venv"]
            ):
                raise ValueError("Installation belongs to a different task venv")
            if tool_id == "frontend_install" and not all(
                value.get("imports", {}).get(name, {}).get("sha256")
                for name in ("triton", "triton_anchor")
            ):
                raise ValueError(
                    "Installation did not verify actual frontend import origins"
                )
            if tool_id == "backend_install":
                discovery = document("backend_discovery", "backend_discovery.json")
                identity(discovery)
                if discovery.get("status") != "pass":
                    raise ValueError("Backend was not discovered after install")
                result["details"]["backend_discovery"] = discovery
            result["details"]["installation"] = value
            result["subject"]["wheel_sha256"] = value["sha256"]
        elif tool_id in {"frontend_tests", "backend_tests"}:
            result["details"]["tests"] = test_report()
            origin = document("import_origin", "import-origin.json")
            identity(origin)
            if origin.get("status") != "pass":
                raise ValueError("Test process import verification failed")
            expected = context.get("expected_imports", {})
            if not expected or any(
                origin.get("imports", {}).get(name) != expected.get(name)
                for name in ("triton", "triton_anchor")
            ):
                raise ValueError(
                    "Test process imports do not match the current installed wheel"
                )
            result["details"]["import_origin"] = origin
        elif tool_id in {"frontend_smoke", "backend_smoke"}:
            value = document("smoke", "smoke_success.json")
            identity(value)
            if value.get("status") != "passed" or value.get("tool") != tool_id:
                raise ValueError("Smoke report did not confirm the selected tool")
            result["details"]["smoke"] = value
        elif tool_id == "control_plane":
            value = document("control_plane", "control_plane.json")
            identity(value)
            if value.get("status") != "pass" or value.get("diff_check") != "pass":
                raise AssertionError("Changed-file contracts failed")
            if value.get("regression_required"):
                result["details"]["tests"] = test_report()
            result["details"]["control_plane"] = value
        elif tool_id == "flaggems":
            value = document("flaggems", "flaggems-summary.json")
            result["details"]["flaggems"] = value
            summary = value.get("summary", {})
            if (
                summary.get("status") != "pass"
                or not summary.get("total")
                or summary.get("passed") != summary.get("total")
            ):
                raise AssertionError(
                    "FlagGems has missing, failed or skipped operators"
                )
            if parameters.get("mode") == "full" and value.get("mode") != "full":
                raise AssertionError("Full FlagGems coverage is required")
            rows = value.get("results", [])
            if len(rows) != summary["total"]:
                raise ValueError("FlagGems summary lacks individual operator evidence")
            result["details"]["flaggems"] = value
            result["scope"].update(
                mode=value.get("mode"), ops=[row["op"] for row in rows]
            )
        elif tool_id in {"compile_time", "pass_profile", "ir_serialization"}:
            value = document("candidate", "candidate.json")
            kernels = (
                parameters.get("kernels")
                or value.get("metadata", {}).get("kernels")
                or list(value.get("summary", {}))
            )
            validate_measurements(tool_id, value, kernels)
            metadata = value.get("metadata", {})
            if metadata.get("commit_sha") != context.get("target_sha"):
                raise ValueError("Measurement belongs to another tested commit")
            if not metadata.get("environment_fingerprint") or metadata[
                "environment_fingerprint"
            ] != context.get("environment_fingerprint"):
                raise ValueError("Measurement belongs to another environment")
            comparison = document("comparison", "comparison.json")
            if comparison.get("status") not in {"pass", "warning", "not_comparable"}:
                raise ValueError("Comparison has no valid measurement conclusion")
            if comparison["status"] != "not_comparable":
                if comparison.get("candidate_sha") != context.get(
                    "target_sha"
                ) or comparison.get("base_sha") != context.get("base_sha"):
                    raise ValueError("Comparison belongs to another candidate or base")
            result["details"]["performance"] = comparison
            result["details"]["measurements"] = {
                "summary": value["summary"],
                "metadata": value.get("metadata", {}),
            }
            result["scope"]["kernels"] = kernels
        else:
            raise ValueError("Unknown evidence tool: " + tool_id)
    except AssertionError as exc:
        if not exit_code:
            result.update(status="fail", reason=str(exc))
        else:
            result["details"]["report_error"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
        if not exit_code:
            result.update(status="infra_error", reason=str(exc))
        else:
            result["details"]["report_error"] = str(exc)
    return result
