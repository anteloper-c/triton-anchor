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

COMPARE = load_script('compare_ir_serialization.py')
BENCHMARK = load_script('ir_serialization_benchmark.py')


def benchmark_document(values, *, generated_at="2026-07-20T00:00:00Z"):
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
    return COMPARE.compare(
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
    base = benchmark_document({"add": {"serialize": 1.0, "deserialize": 2.0}})
    candidate = benchmark_document({"add": {"serialize": 1.1, "deserialize": 2.2}})

    result = compare(base, candidate)

    assert result["status"] == "pass"
    assert result["warnings"] == []


def test_ir_serialization_comparison_warns_on_slowdown():
    base = benchmark_document({"add": {"serialize": 1.0, "deserialize": 2.0}})
    candidate = benchmark_document({"add": {"serialize": 1.25, "deserialize": 2.0}})

    result = compare(base, candidate)

    assert result["status"] == "warning"
    assert result["rows"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]


def test_ir_serialization_comparison_ignores_speedup_and_small_noise():
    base = benchmark_document({"add": {"serialize": 1.0, "deserialize": 0.01}})
    candidate = benchmark_document({"add": {"serialize": 0.5, "deserialize": 0.02}})

    result = compare(base, candidate)

    assert result["status"] == "pass"
    assert all(not row["exceeds_threshold"] for row in result["rows"])


def test_ir_serialization_comparison_warns_when_baseline_missing():
    candidate = benchmark_document({"add": {"serialize": 1.0, "deserialize": 2.0}})

    result = compare(None, candidate)

    assert result["status"] == "warning"
    assert result["baseline_available"] is False
    assert "No cached IR serialization baseline" in result["warnings"][0]


def test_ir_serialization_summary_statistics():
    summary = BENCHMARK.summarize([1.0, 2.0, 3.0])

    assert summary["count"] == 3
    assert summary["median_ms"] == 2.0
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 3.0
