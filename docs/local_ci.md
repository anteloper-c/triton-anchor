# Local CI Runner

This runner is for the internal server path where GitHub Actions cannot reach company dependencies or long-running backend tests.

The Docker container and backend environment are assumed to be ready already. Local CI only does the moving part for each frontend commit:

```text
GitHub push/PR
  -> dispatch exact head SHA to Gitee CI relay ci/* task ref
  -> for a PR, also dispatch its base SHA to ci/base/pr-<number>
  -> poll Gitee CI relay
  -> enter existing Docker
  -> delete the old frontend checkout and fresh-clone the exact task ref
  -> uninstall the old frontend distribution, then build/install from scratch
  -> run triton-anchor/tests/test_smoke.py
  -> rebuild backend wheel against the newly installed frontend
  -> source backend env
  -> run backend smoke/JIT and optional FlagGems
  -> benchmark add, mm, softmax, and layernorm compile time
  -> quantify TTIR serialization/deserialization overhead
  -> compare PR head against the cached result for its base SHA
  -> publish selected logs to local-ci-results in the same relay repository
  -> receiver writes the result to GitHub commit status
```

The Gitee CI relay is intentionally separate from the normal source mirror. One relay repository carries both sides of the protocol without mixing their branches:

```text
ci/push/<github-branch>   exact SHA dispatched by a GitHub push
ci/pr-<number>/<branch>   exact PR head SHA dispatched by a GitHub PR event, including fork PRs
ci/base/pr-<number>       exact PR base SHA; metadata only, not a standalone task
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

Keep the runner checkout separate from the code checkout under test. The runner checkout tracks the trusted branch that owns `scripts/local_ci`; set `LOCAL_CI_SCRIPT_DIR` to that fixed script directory. For each task, the host poller copies `LOCAL_CI_SCRIPT_DIR` into a per-run snapshot under `LOCAL_CI_STATE_DIR/runner/<run-id>/`, then `run_in_container.sh` copies that snapshot into the Docker container. The container path `/workspace/triton-anchor` is deleted and freshly cloned from the dispatched `ci/*` task branch for every PR, push, or full run. PR branches do not need to contain local CI scripts.

Prepared inside the container:

```text
/workspace/llvm-release
/workspace/ppl-release
/workspace/triton-anchor          recreated by every task
/workspace/triton-sophgo-backend
/workspace/FlagGems
```

The runner does not pull backend source code. Backend source and dependencies must already exist in the container, but the backend wheel is rebuilt for every tested frontend commit. `ANCHOR_DIR` must be a dedicated child of `WORKSPACE` and must not overlap the backend, FlagGems, LLVM, PPL, or artifact directories because it is recursively removed before each fresh clone. For another backend, prepare it in the container first, then change `BACKEND_PATH`, `BACKEND_ENVSETUP_ARGS`, and the test commands in `scripts/local_ci/config.env`.

For a PR, the poller reads `ci/base/pr-<number>`. If
the SHA-indexed compile-time, pass-profile, and IR-serialization baselines
already exist on `local-ci-results`, it reuses them. Otherwise it runs the base
commit once, publishes the missing performance baselines, and then runs the PR
head. The base ref is excluded from normal branch discovery, so it does not
create a second independent GitHub CI status.

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
FLAGGEMS_SAMPLE_SIZE="8"
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

Compile-time regression defaults:

```bash
RUN_COMPILE_BENCHMARK="true"
COMPILE_BENCHMARK_KERNELS="add,mm,softmax,layernorm"
COMPILE_BENCHMARK_REPEAT="5"
COMPILE_BENCHMARK_WARMUP="1"
COMPILE_BENCHMARK_THRESHOLD="0.20"
COMPILE_BENCHMARK_TIMEOUT="30m"
```

The threshold is symmetric: a change greater than `+20%` or less than `-20%`
is reported as a warning. A missing base result is also a warning. Correctness,
build, or benchmark execution failures still fail local CI. GitHub commit
statuses have no warning state, so a warning is published as `success` with
the description `Gitee local CI passed with compile-time warning`; the detailed
comparison is linked from the Gitee result directory.

IR serialization regression defaults:

```bash
RUN_IR_SERIALIZATION_BENCHMARK="true"
IR_SERIALIZATION_KERNELS="add,mm,softmax,layernorm"
IR_SERIALIZATION_REPEAT="20"
IR_SERIALIZATION_WARMUP="3"
IR_SERIALIZATION_METRICS="serialize,deserialize"
IR_SERIALIZATION_THRESHOLD="0.20"
IR_SERIALIZATION_MIN_BASE_MS="0.05"
IR_SERIALIZATION_MIN_DELTA_MS="0.05"
IR_SERIALIZATION_TIMEOUT="30m"
```

This comparison is slowdown-only. It warns when a selected median grows by
more than 20%, provided the base median and absolute increase exceed the noise
floors. Missing base data is a warning. See
[`ir_serialization_profiling.md`](ir_serialization_profiling.md) for the exact
measurement boundary.

Existing server installations must update `scripts/local_ci/config.env`; changing `config.example.env` does not overwrite a local configuration. In particular, point `GITEE_REPO_URL` at the relay repository and enable the `ci/*` filter above.

Set `GITEE_TOKEN` for a private relay and for result publishing. The token needs read/write access to the relay repository. The old Gitee commit status API route is not used because Gitee rejects that endpoint with HTTP 405.

For automatic fork PRs, do not expose the write-capable `GITEE_TOKEN` to the Docker container that runs PR code. Leave `LOCAL_CI_ALLOW_WRITE_TOKEN_IN_CONTAINER=0`. If the relay repository is private, set `LOCAL_CI_CONTAINER_GITEE_TOKEN` to a read-only token that can fetch `ci/*`; if the relay is public/readable, leave it empty.

The runner activates `/opt/venv/bin/activate`, explicitly uninstalls the existing `triton-anchor` distribution, sources `envsetup.sh` from the fresh checkout, removes local build/dist metadata, and only then builds and installs the new wheel. This prevents a failed build or stale CMake output from silently falling back to the previous frontend. Set `PYTHON_VENV_ACTIVATE` to another path, or empty, if a different container layout is used.

Set `RUN_FLAGGEMS_TESTS=true` to run the local FlagGems check. Regular local CI uses `FLAGGEMS_TEST_MODE=sample`: it selects one operator from each of the 8 categories in the 59-op pass whitelist in `flaggems_pass_whitelist.tsv`. Values of `FLAGGEMS_SAMPLE_SIZE` below the category count are raised to the category count; values above it add more randomly selected whitelist operators. Sample and full modes both discover pytest markers and test files from the checked-out FlagGems tree before invoking the same per-operator command with `--ref cpu -vs`. Manual full runs use `FLAGGEMS_TEST_MODE=full` through the `ci/full/*` task ref and run attachment-5 operators 1-127 from `flaggems_all_ops.tsv`. Set `FLAGGEMS_TEST_COMMAND` to bypass the selector completely.

Each operator has a 300-second idle timeout based on log growth. Sample and single modes keep a strict 6000-second total timeout. Full mode treats 6000 seconds as a soft deadline: when at least one new pytest node completed since the previous deadline, it extends the deadline by 1800 seconds, up to the absolute 14400-second hard limit. The JSON and Markdown reports include the observed completed-node count, timeout reason, and extension count.

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

Published results are stored on `local-ci-results` under
`runs/<safe-task-ref>/<commit>/<run-id>/`. The result directory keeps selected
build, smoke/JIT, FlagGems, compile-time, pass-profile, and IR-serialization artifacts, including
`flaggems-selected.txt`. Full local logs remain under
`/workspace/local-ci-artifacts`. If the container artifact directory cannot be
mapped back to the host, the publisher writes a fallback summary plus the
host-side `local-ci.log` and `result.json`.

Compile-time artifacts include `compile-benchmark.json`,
`compile-benchmark.csv`, and, for PRs, `compile-time-comparison.json` and
`compile-time-comparison.md`. Pass-profile artifacts include
`pass-profile.json`, its event/summary CSV files, hotspot report, and PR
comparison reports. Stable SHA-indexed baseline copies are written to:

```text
compile-time/by-sha/<commit>/<backend-profile>/latest.json
compile-time/by-sha/<commit>/<backend-profile>/latest.csv
pass-profile/by-sha/<commit>/<backend-profile>/latest.json
ir-serialization/by-sha/<commit>/<backend-profile>/latest.json
ir-serialization/by-sha/<commit>/<backend-profile>/latest.csv
ir-serialization/by-sha/<commit>/<backend-profile>/latest.md
```

The publisher rebuilds `ir-serialization/dashboard.md` and
`ir-serialization/dashboard.csv` on every result publication. The dashboard
lists recent per-kernel medians across SHA and backend profile, with links to
the immutable SHA-indexed JSON data.

These directories are parallel to the existing `runs/` directory. Existing
result repositories do not need migration.

## GitHub Workflows

`IR Serialization Performance Regression Contract` is a lightweight
GitHub-hosted job in `delivery-ci.yml`. It validates the comparison, cache, and
dashboard code without requiring Sophgo dependencies. The actual performance
measurement runs in the prepared local server container and is reported by the
existing Gitee result receiver.

`Dispatch Local CI via Gitee` is the only automatic push/PR entry point.
Pushes to `main` and `jiwang-delivery-ci` create `ci/push/*`; PR events,
including fork PRs, create `ci/pr-*/*` and update the matching
`ci/base/pr-*` pointer. It uses `pull_request_target` so the trusted base-branch
workflow can access the Gitee relay credentials, but it only fetches and relays
the exact PR head/base commits; it must not run PR code on the GitHub-hosted
runner. Manual dispatch with `flaggems_mode=full` creates `ci/full/*` and
reports to the separate `${LOCAL_CI_CONTEXT}/full` status context.

`Receive Local CI Result` polls the existing result protocol and writes `pending`, `success`, or `failure` to the original GitHub SHA. A receiver waits up to 20,400 seconds by default, then starts the next attempt. Four attempts preserve the coworker workflow's long-running handoff behavior without changing the local runner.

Configure these GitHub repository variables if the defaults change:

```text
GITEE_RESULTS_OWNER=likehupochuan
GITEE_RESULTS_REPO=triton-anchor-local-ci-results
GITEE_RESULTS_REPO_URL=https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git
GITEE_RESULTS_BRANCH=local-ci-results
GITEE_RESULTS_WEB_URL=https://gitee.com/likehupochuan/triton-anchor-local-ci-results
LOCAL_CI_CONTEXT=local-ci/sophgo-cmodel
LOCAL_CI_RECEIVER_REF=main
LOCAL_CI_RECEIVER_WAIT_SECONDS=20400
LOCAL_CI_RECEIVER_MAX_ATTEMPTS=4
```

Add GitHub repository secrets `GITEE_TOKEN` and, when it differs from the owner, `GITEE_USERNAME`. The workflow uses GitHub's built-in `GITHUB_TOKEN` with `actions: write` and `statuses: write` to start the receiver and publish commit statuses.

## Order Notes

It is fine for backend source and heavy dependencies to be prepared before the frontend is pulled. The per-commit operation is fresh frontend clone, old frontend uninstall, clean frontend build/install, frontend smoke, backend rebuild, then backend discovery/smoke/JIT. If a future frontend change breaks backend ABI/API compatibility, the fixed rebuild and smoke/JIT sequence should catch it.
