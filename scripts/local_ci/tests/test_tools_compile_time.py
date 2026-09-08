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

MODULE = load_script('compare_compile_time.py')


def benchmark_document(values):
    return {
        "summary": {
            kernel: {"compile_est": {"median_ms": value}}
            for kernel, value in values.items()
        }
    }


def test_compile_time_comparison_passes_within_threshold():
    kernels = ["add", "mm", "softmax", "layernorm"]
    baseline = benchmark_document({kernel: 100.0 for kernel in kernels})
    candidate = benchmark_document({kernel: 110.0 for kernel in kernels})

    result = MODULE.compare(baseline, candidate, kernels, 0.20, "base", "head")

    assert result["status"] == "pass"
    assert result["warnings"] == []


def test_compile_time_comparison_warns_outside_threshold():
    baseline = benchmark_document({"add": 100.0})
    candidate = benchmark_document({"add": 125.0})

    result = MODULE.compare(baseline, candidate, ["add"], 0.20, "base", "head")

    assert result["status"] == "warning"
    assert result["kernels"][0]["exceeds_threshold"] is True
    assert "+25.0%" in result["warnings"][0]


def test_compile_time_comparison_warns_when_baseline_missing():
    candidate = benchmark_document({"add": 100.0})

    result = MODULE.compare(None, candidate, ["add"], 0.20, "base", "head")

    assert result["status"] == "warning"
    assert result["baseline_available"] is False
    assert "No cached compile-time baseline" in result["warnings"][0]
