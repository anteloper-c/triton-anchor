from pathlib import Path
import os
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


def test_runner_requires_the_actual_task_ref() -> None:
    env = os.environ.copy()
    env.pop("GITEE_BRANCH", None)
    env["LOCAL_CI_SCRIPT_STAGED"] = "1"
    result = subprocess.run(
        ["bash", str(ROOT / "deterministic_ci/run_deterministic_ci.sh"), "a" * 40],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "GITEE_BRANCH must be provided by the Local CI poller" in result.stderr


def test_single_branch_poller_requires_an_explicit_branch_list() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env = os.environ.copy()
        env.update(
            {
                "LOCAL_CI_STATE_DIR": directory,
                "LOCAL_CI_ONCE": "1",
                "GITEE_POLL_ALL_BRANCHES": "0",
                "GITEE_BRANCHES": "",
                "LOCAL_CI_CONFIG": str(Path(directory) / "missing.env"),
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "poll_gitee_and_run.sh")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode != 0
    assert "GITEE_BRANCHES is required" in result.stderr
