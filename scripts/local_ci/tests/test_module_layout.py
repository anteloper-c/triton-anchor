from pathlib import Path


LOCAL_CI_ROOT = Path(__file__).resolve().parents[1]

CANONICAL_PATHS = (
    "poll_gitee_and_run.sh",
    "orchestration/run_deterministic_ci_in_container.sh",
    "orchestration/fetch_task_metadata.sh",
    "deterministic_ci/run_deterministic_ci.sh",
    "deterministic_ci/flaggems/batch_test_flaggems.py",
    "deterministic_ci/flaggems/select_flaggems_tests.py",
    "deterministic_ci/performance/compile_benchmark.py",
    "deterministic_ci/performance/compare_compile_time.py",
    "deterministic_ci/performance/pass_profile_benchmark.py",
    "deterministic_ci/performance/compare_pass_profile.py",
    "deterministic_ci/performance/ir_serialization_benchmark.py",
    "deterministic_ci/performance/compare_ir_serialization.py",
    "codex_ai/run_codex_ai_ci.sh",
    "codex_ai/prepare_codex_checkout.sh",
    "codex_ai/setup_codex_ai_container.sh",
    "codex_ai/validate_codex_ai_credentials.py",
    "codex_ai/render_codex_ai_report.py",
    "codex_ai/codex_ai_report.schema.json",
    "codex_ai/prompts/codex_ai_success.md",
    "codex_ai/prompts/codex_ai_failure.md",
    "results/publish_gitee_result.py",
    "results/bridge_gitee_to_github_status.py",
    "shared/result_paths.py",
    "shared/finding_locations.py",
    "shared/dump_artifacts.py",
    "shared/task_tmp.py",
    "shared/path_utils.sh",
    "shared/validate_task_metadata.py",
    "deterministic_ci/performance/common.py",
    "upstream_pr_mirror/mirror_upstream_prs.py",
)


def test_canonical_local_ci_modules_exist():
    missing = [path for path in CANONICAL_PATHS if not (LOCAL_CI_ROOT / path).is_file()]
    assert not missing, f"missing canonical Local CI modules: {missing}"


def test_development_guide_is_not_a_runtime_module():
    guide = LOCAL_CI_ROOT / "DEVELOPMENT_GUIDE.md"
    assert guide.is_file()
    assert "DEVELOPMENT_GUIDE.md" not in CANONICAL_PATHS


def test_local_ci_root_has_only_the_stable_poller_entrypoint():
    root_scripts = {
        path.name
        for path in LOCAL_CI_ROOT.iterdir()
        if path.is_file() and path.suffix in {".sh", ".py"}
    }
    assert root_scripts == {"poll_gitee_and_run.sh"}


def test_obsolete_poller_module_is_removed():
    assert not (LOCAL_CI_ROOT / "orchestration" / "poll_gitee_tasks.sh").exists()


def test_shell_runners_use_the_shared_path_normalizer():
    expected_sources = {
        "poll_gitee_and_run.sh": 'source "${LOCAL_CI_ROOT}/shared/path_utils.sh"',
        "deterministic_ci/run_deterministic_ci.sh": (
            'source "${RUNNER_ROOT}/shared/path_utils.sh"'
        ),
    }
    for relative_path, expected_source in expected_sources.items():
        text = (LOCAL_CI_ROOT / relative_path).read_text(encoding="utf-8")
        assert expected_source in text
        assert "safe_path_part() {" not in text
