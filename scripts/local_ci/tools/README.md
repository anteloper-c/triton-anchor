# Local CI callable tools

For a PR task, Codex calls `start_check` through MCP; the trusted supervisor
invokes `bash scripts/local_ci/tools/run_tool.sh TOOL_ID` inside the current
attempt's independent container, bound to its validated image and frozen commit.
Trusted image preparation also invokes these tools during image validation.
Each invocation executes one tool. There is no checkout, network relay, model
call, or pipeline dispatch here.

Codex may use native commands in its separate writable exploration workspace.
Those commands and their private audit records cannot replace formal tool
records or satisfy minimum checks. The supervisor selects execution identity,
parameters and paths; native experiments do not modify the formal candidate/base
installations. See [Local CI](../README.md) for the execution boundaries.

| ID | Operation / dependency |
| --- | --- |
| `environment` | Validate Python/build dependencies, exact LLVM profile, available backend paths and free space. |
| `frontend_build` | Build one frontend wheel; save its absolute path and SHA256 in task state. |
| `wheel_install_import` | Verify saved wheel, install it, check that both Python packages come from that wheel. |
| `frontend_smoke` | Run the checkout's real frontend smoke against the installed wheel. |
| `backend_rebuild` | Build/install the configured backend wheel and verify discovery. |
| `backend_smoke_jit` | Execute the trusted profile's backend smoke/JIT command. |
| `flaggems` | Reproducible category sample plus affected operators, or requested full/single run. |
| `compile_time` | Run real compilation benchmark, validate candidate measurements, compare compatible baseline. |
| `pass_profile` | Run real MLIR pass profiling; missing pass events are failures. |
| `ir_serialization` | Generate TTIR, serialize/parse, verify canonical content and MLIR validity, measure timings. |
| `contract_tests` | Verify the frozen Git diff, documentation/control syntax and workflow shape; run existing candidate CI pytest suites when present. |

The supervisor enforces the minimum policy and dependency closure:
`environment -> frontend_build -> wheel_install_import -> frontend_smoke ->
backend_rebuild -> backend_smoke_jit -> {flaggems, compile_time, pass_profile,
ir_serialization}`. Performance tools use deployed FlagGems sources but do not
require a preceding FlagGems test run. They do not repair missing capabilities or
turn unavailable backend tools into success. Installation and builds are serial.

Required inputs are absolute `ANCHOR_DIR`, `LOCAL_CI_TASK_ROOT`, and a unique
`LOCAL_CI_ARTIFACT_DIR` per invocation. `LOCAL_CI_TOOL_RESULT` defaults to
`LOCAL_CI_ARTIFACT_DIR/result.json` and must remain directly inside that directory.
`LOCAL_CI_TESTED_SHA` (aliases `LOCAL_CI_TARGET_SHA`, `GITHUB_SHA`) must match HEAD.
The supervisor owns task identity and paths; candidates cannot set these inputs.
State survives invocations under `LOCAL_CI_TASK_ROOT/state`; artifacts and logs
are retained outside temporary benchmark workdirs. No shared caches are deleted.

The trusted profile supplies `LLVM_BUILD_DIR`, `LOCAL_CI_LLVM_HASH`, optional
`PYTHON_VENV_ACTIVATE`/`TRUSTED_ANCHOR_ENVSETUP`, `PYTHON_BIN`, and `PACKAGE_TOOL`
(`auto`, `pip`, `uv`). Candidate `envsetup.sh` is never sourced implicitly.
Backend tools additionally require `RUN_BACKEND_STAGES=true`, `BACKEND_PATH`,
`EXPECTED_TRITON_BACKEND`, `BACKEND_WHEEL_PATTERN`, `BACKEND_TEST_COMMAND`, and
the deployed `FLAGGEMS_CLONE_DIR`/`PPL_ROOT`; optional `BACKEND_ENVSETUP` and
`BACKEND_ENVSETUP_ARGS` come from that same trusted profile.
Environment setup preserves the executor's task venv, source/backend paths,
artifact identity, caches and concurrency budget. Seed Python must include
PyYAML for YAML contracts and pytest for candidate CI suites.

`contract_tests` requires `LOCAL_CI_BASE_SHA` and writes `contracts.json` with
the actual changed paths, hashes and checks. Main-like repositories without a
`scripts/` directory receive real diff/UTF-8/conflict and workflow contracts;
missing CI test directories are recorded. Invalid YAML, empty workflows,
unsupported empty validation, or an existing pytest suite collecting no tests
are failures. Candidate Python is parsed without importing it during syntax checks.

Defaults: fresh frontend build, `MAX_JOBS=8`, minimum free space 5 GiB,
`LOCAL_CI_TOOL_TIMEOUT_SECONDS=7200`. The supervisor may reduce concurrency after
an observed OOM. `FRONTEND_BUILD_MODE=incremental` is an explicit supported
override; it preserves only frontend `build/`. All destructive output cleanup is
restricted to direct build/dist/egg-info children and rejects symlink/mount roots.

FlagGems requires a supervisor-provided `FLAGGEMS_RANDOM_SEED`; supports
`FLAGGEMS_AFFECTED_OPS` (comma-separated), `FLAGGEMS_TEST_MODE`,
`FLAGGEMS_SAMPLE_SIZE`, `FLAGGEMS_TEST_OP`, trusted whitelist/full-list paths,
and existing pytest/timeout settings. Selection, mappings, seed and actual
commands are artifacts. An unknown required operator or empty test selection
fails. `full` authorization belongs to the supervisor.

Performance uses existing `COMPILE_BENCHMARK_*`, `PASS_PROFILE_*`, and
`IR_SERIALIZATION_*` kernel/repeat/warmup/threshold inputs. `BASELINE_JSON` is
optional. Comparisons require matching `LOCAL_CI_ENVIRONMENT_FINGERPRINT` and
`LOCAL_CI_BASE_SHA`; absent/invalid/incompatible baselines report
`not_comparable`. A valid slowdown reports a warning without failing the tool;
invalid candidate measurements, no pass events, or invalid roundtrips fail.
When no explicit fingerprint is supplied, the environment tool derives one from
the toolchain, profile, installed build dependencies and deployed backend refs.

Exit code is authoritative. Enhanced `result.json` includes tool ID, tested SHA,
fingerprint, command argv/cwd/log/exit, artifacts and performance details.
Command failure codes are preserved; timeout is 124, cancellation 130/143, and
configuration/preparation failure normally 2. A supervisor must still validate
its own execution records rather than trust candidate-writable result files.

Boundary verification (no backend/model required):

```sh
python3 -m unittest discover -s scripts/local_ci/tools/tests -v
```
