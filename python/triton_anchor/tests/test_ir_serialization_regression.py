import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_script(relative_path):
    path = ROOT / "scripts" / "local_ci" / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


COMPARE = load_script("deterministic_ci/performance/compare_ir_serialization.py")
BENCHMARK = load_script("deterministic_ci/performance/ir_serialization_benchmark.py")
PUBLISH = load_script("results/publish_gitee_result.py")


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


def test_publish_ir_cache_and_dashboard(tmp_path):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    document = benchmark_document(
        {
            "add": {
                "serialize": 1.0,
                "deserialize": 2.0,
                "roundtrip": 3.5,
            }
        }
    )
    (result_dir / "ir-serialization.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    (result_dir / "ir-serialization.csv").write_text(
        "kernel,serialize_ms\nadd,1.0\n", encoding="utf-8"
    )
    (result_dir / "ir-serialization-summary.md").write_text(
        "# result\n", encoding="utf-8"
    )

    cache_dir = PUBLISH.publish_ir_serialization_cache(tmp_path, result_dir, "abc123")
    markdown_path, csv_path = PUBLISH.write_ir_serialization_dashboard(tmp_path)

    assert cache_dir == (
        tmp_path / "ir-serialization" / "by-sha" / "abc123" / "sophgo-cmodel"
    )
    assert (cache_dir / "latest.json").is_file()
    assert (cache_dir / "latest.csv").is_file()
    assert (cache_dir / "latest.md").is_file()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "abc123" in markdown
    assert "add" in markdown
    assert "2.000" in markdown
    assert csv_path.read_text(encoding="utf-8").startswith("generated_at,sha")
