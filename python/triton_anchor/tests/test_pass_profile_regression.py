import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

PROFILE_SCRIPT = ROOT / "scripts" / "local_ci" / "pass_profile_benchmark.py"
PROFILE_SPEC = importlib.util.spec_from_file_location(
    "pass_profile_benchmark", PROFILE_SCRIPT
)
PROFILE_MODULE = importlib.util.module_from_spec(PROFILE_SPEC)
assert PROFILE_SPEC and PROFILE_SPEC.loader
PROFILE_SPEC.loader.exec_module(PROFILE_MODULE)

COMPARE_SCRIPT = ROOT / "scripts" / "local_ci" / "compare_pass_profile.py"
COMPARE_SPEC = importlib.util.spec_from_file_location(
    "compare_pass_profile", COMPARE_SCRIPT
)
COMPARE_MODULE = importlib.util.module_from_spec(COMPARE_SPEC)
assert COMPARE_SPEC and COMPARE_SPEC.loader
COMPARE_SPEC.loader.exec_module(COMPARE_MODULE)

PUBLISH_SCRIPT = ROOT / "scripts" / "local_ci" / "publish_gitee_result.py"
PUBLISH_SPEC = importlib.util.spec_from_file_location(
    "publish_gitee_result", PUBLISH_SCRIPT
)
PUBLISH_MODULE = importlib.util.module_from_spec(PUBLISH_SPEC)
assert PUBLISH_SPEC and PUBLISH_SPEC.loader
PUBLISH_SPEC.loader.exec_module(PUBLISH_MODULE)


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


def test_publish_pass_profile_cache_uses_sha_and_profile(tmp_path):
    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "pass-profile.json").write_text(
        '{"metadata":{"backend_profile":"sophgo-cmodel"},"summary":{}}',
        encoding="utf-8",
    )
    (result_dir / "pass-profile-summary.csv").write_text(
        "kernel,pass,median_ms\n", encoding="utf-8"
    )

    cache_dir = PUBLISH_MODULE.publish_pass_profile_cache(
        tmp_path, result_dir, "abc123"
    )

    assert (
        cache_dir == tmp_path / "pass-profile" / "by-sha" / "abc123" / "sophgo-cmodel"
    )
    assert (cache_dir / "latest.json").is_file()
    assert (cache_dir / "latest-summary.csv").is_file()
