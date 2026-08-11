import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
CLASSIFIER_PATH = (
    REPO_ROOT
    / "scripts"
    / "local_ci"
    / "codex_ai"
    / "classify_codex_review_context.py"
)

SPEC = importlib.util.spec_from_file_location("codex_review_context", CLASSIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
CLASSIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLASSIFIER)


def changed(path: str, change_type: str = "added") -> dict[str, str]:
    return {"path": path, "change_type": change_type}


def test_pr28_style_workflows_use_local_ci_control_profile():
    manifest = [
        changed(".github/workflows/api-breaking-notify.yml"),
        changed(".github/workflows/ci-gateway.yml"),
    ]

    profile, hint, summary = CLASSIFIER.classify_review_context(manifest, "full")

    assert profile == "local_ci_control"
    assert summary["profile"] == "local_ci_control"
    assert summary["file_count"] == 2
    assert summary["groups"] == {
        "github_workflows": [
            ".github/workflows/api-breaking-notify.yml",
            ".github/workflows/ci-gateway.yml",
        ]
    }
    assert summary["change_types"] == {"added": 2}
    for required_term in [
        "事件覆盖",
        "workflow/artifact 契约",
        "特权事件",
        "不可信输入边界",
    ]:
        assert required_term in hint


def test_workflow_and_local_ci_changes_use_local_ci_control_profile():
    manifest = [
        changed(".github/workflows/ci-gateway.yml", "modified"),
        changed("scripts/local_ci/poll_gitee_and_run.sh", "modified"),
        changed("scripts/local_ci/tests/test_poll_gitee_and_run.py", "modified"),
    ]

    profile, _, summary = CLASSIFIER.classify_review_context(manifest, "full")

    assert profile == "local_ci_control"
    assert set(summary["groups"]) == {
        "github_workflows",
        "local_ci_control",
    }


def test_failure_analysis_keeps_failure_profile_and_workflow_group():
    manifest = [changed(".github/workflows/ci-gateway.yml", "modified")]

    profile, hint, summary = CLASSIFIER.classify_review_context(
        manifest, "analysis_only"
    )

    assert profile == "local_ci_failure"
    assert "失败阶段" in hint
    assert summary["groups"] == {
        "github_workflows": [".github/workflows/ci-gateway.yml"]
    }


def test_large_workflow_only_diff_uses_large_diff_profile():
    manifest = [
        changed(f".github/workflows/gateway-{index}.yml") for index in range(21)
    ]

    profile, _, summary = CLASSIFIER.classify_review_context(manifest, "full")

    assert profile == "large_diff"
    assert summary["file_count"] == 21


def test_codex_prompt_markdown_is_classified_as_codex_ai_maintenance():
    manifest = [
        changed("scripts/local_ci/codex_ai/prompts/codex_ai_success.md", "modified")
    ]

    profile, _, summary = CLASSIFIER.classify_review_context(manifest, "full")

    assert profile == "codex_ai_ci_maintenance"
    assert set(summary["groups"]) == {"codex_ai"}
