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

COMPILE_COMPARE = load_script('compare_compile_time.py')


def compile_document(values):
    return {
        "summary": {
            kernel: {"compile_est": {"median_ms": value}}
            for kernel, value in values.items()
        }
    }


def test_compile_time_comparison_passes_within_threshold():
    kernels = ["add", "mm", "softmax", "layernorm"]
    baseline = compile_document({kernel: 100.0 for kernel in kernels})
    candidate = compile_document({kernel: 110.0 for kernel in kernels})

    result = COMPILE_COMPARE.compare(baseline, candidate, kernels, 0.20, "base", "head")

    assert result["status"] == "pass"
    assert result["warnings"] == []


def test_compile_time_comparison_warns_outside_threshold():
    baseline = compile_document({"add": 100.0})
    candidate = compile_document({"add": 125.0})

    result = COMPILE_COMPARE.compare(baseline, candidate, ["add"], 0.20, "base", "head")

    assert result["status"] == "warning"
    assert result["kernels"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]


def test_compile_time_comparison_warns_when_baseline_missing():
    candidate = compile_document({"add": 100.0})

    result = COMPILE_COMPARE.compare(None, candidate, ["add"], 0.20, "base", "head")

    assert result["status"] == "warning"
    assert result["baseline_available"] is False
    assert "No cached compile-time baseline" in result["warnings"][0]

PASS_BENCHMARK = load_script('pass_profile_benchmark.py')
PASS_COMPARE = load_script('compare_pass_profile.py')


def pass_document(values):
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

    events = PASS_BENCHMARK.parse_timing_output(text, "add", "repeat", "0")

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

    summary = PASS_BENCHMARK.build_summary(["add"], run_results, run_events, top_n=2)

    assert summary["add"]["hotspots"][0]["name"] == "slow"
    assert summary["add"]["passes"]["slow"]["wall_ms"]["median_ms"] == 5.0


def test_pass_profile_comparison_warns_on_slowdown():
    baseline = pass_document({"triton_to_linalg": 10.0})
    candidate = pass_document({"triton_to_linalg": 13.0})

    result = PASS_COMPARE.compare(
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
    baseline = pass_document({"tiny": 0.5})
    candidate = pass_document({"tiny": 2.0})

    result = PASS_COMPARE.compare(
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

IR_COMPARE = load_script('compare_ir_serialization.py')
IR_BENCHMARK = load_script('ir_serialization_benchmark.py')


def ir_document(values, *, generated_at="2026-07-20T00:00:00Z"):
    summary = {}
    for kernel, metrics in values.items():
        summary[kernel] = {
            "module_count": 1,
            "ir_bytes": 1024,
            "metrics": {
                metric: {"median_ms": value} for metric, value in metrics.items()
            },
        }
    return {
        "metadata": {
            "backend_profile": "sophgo-cmodel",
            "generated_at": generated_at,
        },
        "summary": summary,
    }


def compare(base, candidate, *, min_base_ms=0.05, min_delta_ms=0.05):
    return IR_COMPARE.compare(
        base,
        candidate,
        ["add"],
        ["serialize", "deserialize"],
        0.20,
        min_base_ms,
        min_delta_ms,
        "base",
        "head",
    )


def test_ir_serialization_comparison_passes_within_threshold():
    base = ir_document({"add": {"serialize": 1.0, "deserialize": 2.0}})
    candidate = ir_document({"add": {"serialize": 1.1, "deserialize": 2.2}})

    result = compare(base, candidate)

    assert result["status"] == "pass"
    assert result["warnings"] == []


def test_ir_serialization_comparison_warns_on_slowdown():
    base = ir_document({"add": {"serialize": 1.0, "deserialize": 2.0}})
    candidate = ir_document({"add": {"serialize": 1.25, "deserialize": 2.0}})

    result = compare(base, candidate)

    assert result["status"] == "warning"
    assert result["rows"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]


def test_ir_serialization_comparison_ignores_speedup_and_small_noise():
    base = ir_document({"add": {"serialize": 1.0, "deserialize": 0.01}})
    candidate = ir_document({"add": {"serialize": 0.5, "deserialize": 0.02}})

    result = compare(base, candidate)

    assert result["status"] == "pass"
    assert all(not row["exceeds_threshold"] for row in result["rows"])


def test_ir_serialization_comparison_warns_when_baseline_missing():
    candidate = ir_document({"add": {"serialize": 1.0, "deserialize": 2.0}})

    result = compare(None, candidate)

    assert result["status"] == "warning"
    assert result["baseline_available"] is False
    assert "No cached IR serialization baseline" in result["warnings"][0]


def test_ir_serialization_summary_statistics():
    summary = IR_BENCHMARK.summarize([1.0, 2.0, 3.0])

    assert summary["count"] == 3
    assert summary["median_ms"] == 2.0
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 3.0
