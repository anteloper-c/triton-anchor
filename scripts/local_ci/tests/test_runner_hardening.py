from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "deterministic_ci/run_deterministic_ci.sh").read_text()
POLLER = (ROOT / "poll_gitee_and_run.sh").read_text()


def test_pr_sources_only_base_envsetup_from_runner_snapshot() -> None:
    assert 'TRUSTED_ANCHOR_ENVSETUP="${TRUSTED_ANCHOR_ENVSETUP:-${RUNNER_ROOT}/trusted/envsetup.sh}"' in RUNNER
    assert 'bash -n "${ANCHOR_DIR}/envsetup.sh"' in RUNNER
    assert 'source "${TRUSTED_ANCHOR_ENVSETUP}"' not in RUNNER
    assert 'source "${envsetup_file}"' in RUNNER
    assert 'git -C "${checkout_dir}" show "${base_sha}:envsetup.sh"' in POLLER


def test_candidate_exit_zero_cannot_override_required_stage_failure() -> None:
    assert "required_stages_passed()" in RUNNER
    for status in (
        "FRONTEND_BUILD_STATUS",
        "FRONTEND_SMOKE_STATUS",
        "BACKEND_REBUILD_STATUS",
        "BACKEND_SMOKE_JIT_STATUS",
    ):
        assert status in RUNNER
    assert "forcing overall failure" in RUNNER


def test_pr_without_trusted_envsetup_fails_closed() -> None:
    assert "Trusted base commit has no envsetup.sh" in POLLER
    assert "Trusted base envsetup.sh is required for PR Local CI" in RUNNER
    assert "PR Local CI requires SOURCE_ENVSETUP=1" in RUNNER


def test_exit_zero_envsetup_is_never_sourced_from_candidate() -> None:
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "envsetup.sh"
        candidate.write_text("exit 0\n", encoding="utf-8")
        subprocess.run(["bash", "-n", str(candidate)], check=True)

    pr_block = RUNNER[RUNNER.index('if [[ -n "${LOCAL_CI_BASE_SHA}" ]]') :]
    pr_block = pr_block[: pr_block.index("elif")]
    assert 'bash -n "${ANCHOR_DIR}/envsetup.sh"' in pr_block
    assert 'source "${ANCHOR_DIR}/envsetup.sh"' not in pr_block
    assert 'envsetup_file="${TRUSTED_ANCHOR_ENVSETUP}"' in pr_block
