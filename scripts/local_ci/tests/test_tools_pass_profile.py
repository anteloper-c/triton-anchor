"""Preserved performance regression tests for the relocated real tool programs."""
import importlib.util
from pathlib import Path

PERFORMANCE_DIR = Path(__file__).resolve().parents[1] / "tools" / "basic_tools" / "performance"

def load_script(name):
    path = PERFORMANCE_DIR / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

PROFILE_MODULE = load_script('pass_profile_benchmark.py')
COMPARE_MODULE = load_script('compare_pass_profile.py')


def profile_document(values):
    return {
        "summary": {
            "add": {
                "passes": {
                    name: {
                        "wall_ms": {"median_ms": value},
                        "invocations": {"median_ms": 1.0},
                    }
                    for name, value in values.items()
                }
            }
        }
    }


def test_parse_mlir_timing_rows_with_bare_seconds_and_units():
    text = """
===-------------------------------------------------------------------------===
  Total Execution Time: 0.0030 seconds
   ----Wall Time----  ----Name----
   0.0010 ( 33.3%)    'canonicalizer' Pass
   2.5000ms ( 66.7%)      triton_to_linalg Pass
   0.0030 (100.0%)  'builtin.module' Pipeline
"""

    events = PROFILE_MODULE.parse_timing_output(text, "add", "repeat", "0")

    assert len(events) == 3
    assert events[0]["name"] == "canonicalizer"
    assert events[0]["wall_ms"] == 1.0
    assert events[1]["name"] == "triton_to_linalg"
    assert events[1]["wall_ms"] == 2.5
    assert events[2]["kind"] == "pipeline"


def test_pass_profile_summary_sorts_hotspots_by_median():
    events = [
        {
            "kernel": "add",
            "run_id": "0",
            "kind": "pass",
            "name": "slow",
            "wall_ms": 4.0,
        },
        {
            "kernel": "add",
            "run_id": "0",
            "kind": "pass",
            "name": "fast",
            "wall_ms": 1.0,
        },
        {
            "kernel": "add",
            "run_id": "1",
            "kind": "pass",
            "name": "slow",
            "wall_ms": 6.0,
        },
        {
            "kernel": "add",
            "run_id": "1",
            "kind": "pass",
            "name": "fast",
            "wall_ms": 1.5,
        },
    ]
    run_results = [
        {"kernel": "add", "run_id": "0", "compile_est_ms": 10.0, "spec": {}},
        {"kernel": "add", "run_id": "1", "compile_est_ms": 12.0, "spec": {}},
    ]
    run_events = {
        ("add", "0"): events[:2],
        ("add", "1"): events[2:],
    }

    summary = PROFILE_MODULE.build_summary(["add"], run_results, run_events, top_n=2)

    assert summary["add"]["hotspots"][0]["name"] == "slow"
    assert summary["add"]["passes"]["slow"]["wall_ms"]["median_ms"] == 5.0


def test_pass_profile_comparison_warns_on_slowdown():
    baseline = profile_document({"triton_to_linalg": 10.0})
    candidate = profile_document({"triton_to_linalg": 13.0})

    result = COMPARE_MODULE.compare(
        baseline,
        candidate,
        ["add"],
        threshold=0.20,
        min_base_ms=1.0,
        min_delta_ms=1.0,
        top_n=10,
        mode="slowdown",
        base_sha="base",
        candidate_sha="head",
    )

    assert result["status"] == "warning"
    assert result["passes"][0]["exceeds_threshold"] is True
    assert "+30.0%" in result["warnings"][0]


def test_pass_profile_comparison_ignores_tiny_pass_delta():
    baseline = profile_document({"tiny": 0.5})
    candidate = profile_document({"tiny": 2.0})

    result = COMPARE_MODULE.compare(
        baseline,
        candidate,
        ["add"],
        threshold=0.20,
        min_base_ms=1.0,
        min_delta_ms=1.0,
        top_n=10,
        mode="slowdown",
        base_sha="base",
        candidate_sha="head",
    )

    assert result["status"] == "pass"
