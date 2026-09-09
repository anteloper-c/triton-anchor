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


def test_compile_time_comparison_warns_outside_threshold():
    baseline = compile_document({"add": 100.0})
    candidate = compile_document({"add": 125.0})

    result = COMPILE_COMPARE.compare(baseline, candidate, ["add"], 0.20, "base", "head")

    assert result["status"] == "warning"
    assert result["kernels"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]


PASS_BENCHMARK = load_script('pass_profile_benchmark.py')


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


IR_COMPARE = load_script('compare_ir_serialization.py')


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


def test_ir_serialization_comparison_warns_on_slowdown():
    base = ir_document({"add": {"serialize": 1.0, "deserialize": 2.0}})
    candidate = ir_document({"add": {"serialize": 1.25, "deserialize": 2.0}})

    result = compare(base, candidate)

    assert result["status"] == "warning"
    assert result["rows"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]
