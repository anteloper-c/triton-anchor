import importlib.util
from pathlib import Path


CLASSIFIER_PATH = Path(__file__).resolve().parents[1] / "classify_codex_review_context.py"
RUNNER_PATH = Path(__file__).resolve().parents[1] / "run_codex_ai_ci.sh"
SPEC = importlib.util.spec_from_file_location("codex_review_context", CLASSIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
CLASSIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLASSIFIER)


def changed(path: str, change_type: str = "modified") -> dict[str, str]:
    return {"path": path, "change_type": change_type}


def classify(paths, analysis_mode="full"):
    return CLASSIFIER.classify_review_context(paths, analysis_mode)


def test_runner_preflights_and_invokes_the_classifier_path():
    runner = RUNNER_PATH.read_text(encoding="utf-8")
    assert 'review_context_classifier="${SCRIPT_DIR}/classify_codex_review_context.py"' in runner
    assert '[[ ! -r "${review_context_classifier}" ]]' in runner
    assert '"${PYTHON_BIN}" "${review_context_classifier}"' in runner


def test_empty_and_malformed_manifests_use_empty_diff_profile():
    for manifest in ([], {}, None, [None, {"path": 1, "change_type": "added"}]):
        profile, hint, summary = classify(manifest)
        assert profile == "empty_diff"
        assert "未检测到变更文件" in hint
        assert summary["file_count"] == 0
        assert summary["groups"] == {}


def test_codex_ai_only_change_uses_maintenance_profile():
    profile, hint, summary = classify(
        [changed("scripts/local_ci/codex_ai/prompts/codex_ai_success.md")]
    )
    assert profile == "codex_ai_ci_maintenance"
    assert "Codex AI-CI 自身文件" in hint
    assert summary["groups"] == {
        "codex_ai": ["scripts/local_ci/codex_ai/prompts/codex_ai_success.md"]
    }


def test_analysis_only_prioritizes_local_ci_failure():
    profile, hint, summary = classify(
        [changed("python/triton_anchor/language/core.py")], "analysis_only"
    )
    assert profile == "local_ci_failure"
    assert "失败阶段" in hint
    assert set(summary["groups"]) == {"python_frontend"}


def test_docs_protocol_performance_and_control_profiles():
    cases = (
        ([changed("docs/ci_guide_zh.md")], "docs_only", "docs"),
        (
            [changed("scripts/local_ci/shared/result_paths.py")],
            "local_ci_protocol",
            "shared_protocol",
        ),
        (
            [changed("scripts/local_ci/deterministic_ci/performance/common.py")],
            "performance",
            "performance",
        ),
        (
            [changed(".github/workflows/local_ci.yml")],
            "local_ci_control",
            "github_workflows",
        ),
    )
    for manifest, expected_profile, expected_group in cases:
        profile, _, summary = classify(manifest)
        assert profile == expected_profile
        assert set(summary["groups"]) == {expected_group}


def test_large_diff_takes_priority_over_general_profile():
    manifest = [changed(f"csrc/pass_{index}.cpp") for index in range(21)]
    profile, hint, summary = classify(manifest)
    assert profile == "large_diff"
    assert "diff 较大" in hint
    assert summary["file_count"] == 21
    assert set(summary["groups"]) == {"compiler_core"}


def test_general_profile_preserves_sorted_groups_and_change_type_counts():
    profile, _, summary = classify(
        [
            changed("python/triton_anchor/z.py", "added"),
            changed("csrc/a.cpp", "modified"),
            changed("python/triton_anchor/a.py", "added"),
        ]
    )
    assert profile == "general"
    assert summary["schema"] == "triton-anchor-codex-review-context/v1"
    assert summary["groups"] == {
        "compiler_core": ["csrc/a.cpp"],
        "python_frontend": [
            "python/triton_anchor/a.py",
            "python/triton_anchor/z.py",
        ],
    }
    assert summary["change_types"] == {"added": 2, "modified": 1}
