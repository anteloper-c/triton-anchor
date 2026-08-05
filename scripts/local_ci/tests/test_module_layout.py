from pathlib import Path


LOCAL_CI_ROOT = Path(__file__).resolve().parents[1]

CANONICAL_PATHS = (
    "orchestration/poll_gitee_tasks.sh",
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
    "shared/validate_task_metadata.py",
)

SHELL_COMPATIBILITY_ENTRYPOINTS = {
    "poll_gitee_and_run.sh": "orchestration/poll_gitee_tasks.sh",
    "run_in_container.sh": "orchestration/run_deterministic_ci_in_container.sh",
    "run_delivery_local.sh": "deterministic_ci/run_deterministic_ci.sh",
    "fetch_task_metadata.sh": "orchestration/fetch_task_metadata.sh",
    "run_codex_ai_ci.sh": "codex_ai/run_codex_ai_ci.sh",
    "prepare_codex_checkout.sh": "codex_ai/prepare_codex_checkout.sh",
    "setup_codex_ai_container.sh": "codex_ai/setup_codex_ai_container.sh",
}

PYTHON_COMPATIBILITY_ENTRYPOINTS = {
    "batch_test_flaggems.py": "deterministic_ci/flaggems/batch_test_flaggems.py",
    "select_flaggems_tests.py": "deterministic_ci/flaggems/select_flaggems_tests.py",
    "compile_benchmark.py": "deterministic_ci/performance/compile_benchmark.py",
    "compare_compile_time.py": "deterministic_ci/performance/compare_compile_time.py",
    "pass_profile_benchmark.py": "deterministic_ci/performance/pass_profile_benchmark.py",
    "compare_pass_profile.py": "deterministic_ci/performance/compare_pass_profile.py",
    "ir_serialization_benchmark.py": "deterministic_ci/performance/ir_serialization_benchmark.py",
    "compare_ir_serialization.py": "deterministic_ci/performance/compare_ir_serialization.py",
    "publish_gitee_result.py": "results/publish_gitee_result.py",
    "bridge_gitee_to_github_status.py": "results/bridge_gitee_to_github_status.py",
    "render_codex_ai_report.py": "codex_ai/render_codex_ai_report.py",
    "validate_codex_ai_credentials.py": "codex_ai/validate_codex_ai_credentials.py",
    "validate_task_metadata.py": "shared/validate_task_metadata.py",
    "result_paths.py": "shared/result_paths.py",
}


def test_canonical_local_ci_modules_exist():
    missing = [path for path in CANONICAL_PATHS if not (LOCAL_CI_ROOT / path).is_file()]
    assert not missing, f"missing canonical Local CI modules: {missing}"


def test_shell_compatibility_entrypoints_forward_to_canonical_modules():
    for legacy_path, canonical_path in SHELL_COMPATIBILITY_ENTRYPOINTS.items():
        text = (LOCAL_CI_ROOT / legacy_path).read_text(encoding="utf-8")
        assert canonical_path in text
        assert "exec bash" in text


def test_python_compatibility_entrypoints_forward_to_canonical_modules():
    for legacy_path, canonical_path in PYTHON_COMPATIBILITY_ENTRYPOINTS.items():
        text = (LOCAL_CI_ROOT / legacy_path).read_text(encoding="utf-8")
        assert canonical_path in text
        assert "run_legacy_entrypoint" in text
