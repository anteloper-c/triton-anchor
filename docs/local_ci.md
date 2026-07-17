# Local CI Runner

This runner is for the internal server path where GitHub Actions cannot reach company dependencies or long-running backend tests.

The Docker container and backend environment are assumed to be ready already. Local CI only does the moving part for each frontend commit:

```text
GitHub push/PR
  -> dispatch exact SHA to Gitee CI relay ci/* task ref
  -> poll Gitee CI relay
  -> enter existing Docker
  -> checkout/build/install frontend
  -> run triton-anchor/tests/test_smoke.py
  -> rebuild backend wheel against the newly installed frontend
  -> source backend env
  -> run backend smoke/JIT and optional FlagGems
  -> publish selected logs to local-ci-results in the same relay repository
  -> receiver writes the result to GitHub commit status
  -> scheduled bridge reconciles missed status updates
```

The Gitee CI relay is intentionally separate from the normal source mirror. One relay repository carries both sides of the protocol without mixing their branches:

```text
ci/push/<github-branch>   exact SHA dispatched by a GitHub push
ci/pr-<number>/<branch>   exact PR head SHA dispatched by a GitHub PR event, including fork PRs
ci/full/<github-branch>   manual full FlagGems run for a GitHub branch
local-ci-results          local runner results only
```

The mirror process must not target this relay repository.

## Expected Layout

```text
host runner checkout: /opt/local-ci/triton-anchor-runner
host config/state:    /opt/local-ci/config.env, /root/projects/test/local-ci-state
container workspace:  /workspace
```

Keep the runner checkout separate from the code checkout under test. The runner checkout tracks the trusted branch that owns `scripts/local_ci`; set `LOCAL_CI_SCRIPT_DIR` to that fixed script directory. For each task, the host poller copies `LOCAL_CI_SCRIPT_DIR` into a per-run snapshot under `LOCAL_CI_STATE_DIR/runner/<run-id>/`, then `run_in_container.sh` copies that snapshot into the Docker container. The container workspace `/workspace/triton-anchor` is reset to the dispatched `ci/*` task commit for each PR or push. PR branches do not need to contain local CI scripts.

Prepared inside the container:

```text
/workspace/llvm-release
/workspace/ppl-release
/workspace/triton-anchor
/workspace/triton-sophgo-backend
/workspace/FlagGems
```

The runner does not pull backend source code. Backend source and dependencies must already exist in the container, but the backend wheel is rebuilt for every tested frontend commit. For another backend, prepare it in the container first, then change `BACKEND_PATH`, `BACKEND_ENVSETUP_ARGS`, and the test commands in `scripts/local_ci/config.env`.

## Configure

```bash
cd /opt/local-ci/triton-anchor-runner
cp scripts/local_ci/config.example.env /opt/local-ci/config.env
```

Important defaults for Sophgo CModel:

```bash
BACKEND_PROFILE="sophgo-cmodel"
EXPECTED_TRITON_BACKEND="sophgo"
BACKEND_PATH="/workspace/triton-sophgo-backend"
BACKEND_ENVSETUP_ARGS="PIO_CMODEL"
BACKEND_TEST_COMMAND="python3 tests/test_smoke.py && python3 tests/test_jit.py"
PYTHON_VENV_ACTIVATE="/opt/venv/bin/activate"
RUN_FLAGGEMS_TESTS="true"
FLAGGEMS_PIP_PACKAGES="scipy pytest"
FLAGGEMS_TEST_MODE="sample"
FLAGGEMS_SAMPLE_SIZE="6"
FLAGGEMS_RANDOM_SEED=""
FLAGGEMS_TEST_COMMAND=""
```

Use the same independent Gitee repository for task code and results:

```bash
GITEE_REPO_URL="https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git"
GITEE_OWNER="likehupochuan"
GITEE_REPO="triton-anchor-local-ci-results"
GITEE_POLL_ALL_BRANCHES="1"
GITEE_BRANCH_INCLUDE_REGEX="^ci/(pr-[0-9]+/.+|push/.+|full/.+)$"

GITEE_RESULTS_OWNER="likehupochuan"
GITEE_RESULTS_REPO="triton-anchor-local-ci-results"
GITEE_RESULTS_REPO_URL="https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git"
GITEE_RESULTS_BRANCH="local-ci-results"
GITEE_RESULTS_WEB_URL="https://gitee.com/likehupochuan/triton-anchor-local-ci-results"
```

Existing server installations must update `scripts/local_ci/config.env`; changing `config.example.env` does not overwrite a local configuration. In particular, point `GITEE_REPO_URL` at the relay repository and enable the `ci/*` filter above.

Set `GITEE_TOKEN` for a private relay and for result publishing. The token needs read/write access to the relay repository. The old Gitee commit status API route is not used because Gitee rejects that endpoint with HTTP 405.

For automatic fork PRs, do not expose the write-capable `GITEE_TOKEN` to the Docker container that runs PR code. Leave `LOCAL_CI_ALLOW_WRITE_TOKEN_IN_CONTAINER=0`. If the relay repository is private, set `LOCAL_CI_CONTAINER_GITEE_TOKEN` to a read-only token that can fetch `ci/*`; if the relay is public/readable, leave it empty.

The runner activates `/opt/venv/bin/activate` before running `uv build` or `uv pip install`. Set `PYTHON_VENV_ACTIVATE` to another path, or empty, if a different container layout is used.

Set `RUN_FLAGGEMS_TESTS=true` to run the local FlagGems check. Regular local CI uses `FLAGGEMS_TEST_MODE=sample`: it selects `FLAGGEMS_SAMPLE_SIZE=6` operators from the 36-op pass whitelist in `flaggems_pass_whitelist.tsv` with category-balanced sampling based on the attachment-5 `category` column. Manual full runs use `FLAGGEMS_TEST_MODE=full` through the `ci/full/*` task ref and run attachment-5 operators 1-127 from `flaggems_all_ops.tsv`. Set `FLAGGEMS_TEST_COMMAND` to bypass the selector completely.

## Run

Run one discovery pass:

```bash
LOCAL_CI_CONFIG=/opt/local-ci/config.env bash scripts/local_ci/poll_gitee_and_run.sh --once
```

Run continuously:

```bash
LOCAL_CI_CONFIG=/opt/local-ci/config.env LOCAL_CI_POLL_INTERVAL=60 bash scripts/local_ci/poll_gitee_and_run.sh
```

Host-side poller logs/state:

```text
/root/projects/test/local-ci-state
```

Container-side artifacts:

```text
/workspace/local-ci-artifacts
```

Published results are stored on `local-ci-results` under `runs/<safe-task-ref>/<commit>/<run-id>/`. The result directory intentionally keeps only `delivery-summary.txt`, `frontend-install.log`, `frontend-smoke.log`, `backend-rebuild.log`, `backend-smoke-jit.log`, `flaggems.log`, and `flaggems-selected.txt`. Full local logs remain under `/workspace/local-ci-artifacts`.

## GitHub Workflows

`Dispatch Local CI via Gitee` is the only automatic push/PR entry point. Pushes to `main` and `jiwang-delivery-ci` create `ci/push/*`; PR events, including fork PRs, create `ci/pr-*/*`. It uses `pull_request_target` so the trusted base-branch workflow can access the Gitee relay credentials, but it only fetches `refs/pull/<PR>/head` and pushes that exact commit to Gitee; it must not run PR code on the GitHub-hosted runner. Manual dispatch with `flaggems_mode=full` creates `ci/full/*` and reports to the separate `${LOCAL_CI_CONTEXT}/full` status context.

`Receive Local CI Result` polls the existing result protocol and writes `pending`, `success`, or `failure` to the original GitHub SHA. A receiver waits up to 20,400 seconds by default, then starts the next attempt. Four attempts preserve the coworker workflow's long-running handoff behavior without changing the local runner.

`Local CI Bridge` is retained as a manual query and scheduled reconciliation fallback. It checks configured push branch heads and open PRs using the same `ci/*` task-ref mapping, including fork PR heads, so a delayed or cancelled receiver does not permanently lose a final status.

Configure these GitHub repository variables if the defaults change:

```text
GITEE_RESULTS_OWNER=likehupochuan
GITEE_RESULTS_REPO=triton-anchor-local-ci-results
GITEE_RESULTS_REPO_URL=https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git
GITEE_RESULTS_BRANCH=local-ci-results
GITEE_RESULTS_WEB_URL=https://gitee.com/likehupochuan/triton-anchor-local-ci-results
LOCAL_CI_CONTEXT=local-ci/sophgo-cmodel
LOCAL_CI_RECONCILE_SOURCE_BRANCHES="main jiwang-delivery-ci"
LOCAL_CI_BRIDGE_MAX_PRS=100
LOCAL_CI_RECEIVER_REF=main
LOCAL_CI_RECEIVER_WAIT_SECONDS=20400
LOCAL_CI_RECEIVER_MAX_ATTEMPTS=4
```

Add GitHub repository secrets `GITEE_TOKEN` and, when it differs from the owner, `GITEE_USERNAME`. The workflow uses GitHub's built-in `GITHUB_TOKEN` with `actions: write` and `statuses: write` to start the receiver and publish commit statuses.

## Order Notes

It is fine for backend source and heavy dependencies to be prepared before the frontend is pulled. The per-commit operation is frontend checkout/build/install, frontend smoke, backend rebuild, then backend discovery/smoke/JIT. If a future frontend change breaks backend ABI/API compatibility, the fixed rebuild and smoke/JIT sequence should catch it.
