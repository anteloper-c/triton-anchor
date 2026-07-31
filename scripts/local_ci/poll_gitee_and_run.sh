#!/usr/bin/env bash
set -euo pipefail

SOURCE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${LOCAL_CI_CONFIG:-${SOURCE_SCRIPT_DIR}/config.env}"
if [[ -f "${CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi
LOCAL_CI_CONFIG="${CONFIG_FILE}"

LOCAL_CI_STATE_DIR="${LOCAL_CI_STATE_DIR:-/root/projects/test/local-ci-state}"
LOCAL_CI_SCRIPT_DIR="${LOCAL_CI_SCRIPT_DIR:-${SOURCE_SCRIPT_DIR}}"

GITEE_REPO_URL="${GITEE_REPO_URL:-https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git}"
GITEE_OWNER="${GITEE_OWNER:-likehupochuan}"
GITEE_REPO="${GITEE_REPO:-triton-anchor-local-ci-results}"
GITEE_BRANCH="${GITEE_BRANCH:-ci/push/jiwang-delivery-ci}"
GITEE_BRANCHES="${GITEE_BRANCHES:-${GITEE_BRANCH}}"
GITEE_POLL_ALL_BRANCHES="${GITEE_POLL_ALL_BRANCHES:-1}"
GITEE_BRANCH_INCLUDE_REGEX="${GITEE_BRANCH_INCLUDE_REGEX:-^ci/(pr-[0-9]+/.+|push/.+|full/.+)$}"
GITEE_TOKEN="${GITEE_TOKEN:-}"
LOCAL_CI_STATE_DIR="${LOCAL_CI_STATE_DIR:-/root/projects/test/local-ci-state}"
LOCAL_CI_POLL_INTERVAL="${LOCAL_CI_POLL_INTERVAL:-60}"
LOCAL_CI_ONCE="${LOCAL_CI_ONCE:-0}"
GITEE_RESULT_CONTEXT="${GITEE_RESULT_CONTEXT:-local-ci/sophgo-cmodel}"
GITEE_RESULTS_BRANCH="${GITEE_RESULTS_BRANCH:-local-ci-results}"
GITEE_RESULTS_OWNER="${GITEE_RESULTS_OWNER:-${GITEE_OWNER}}"
GITEE_RESULTS_REPO="${GITEE_RESULTS_REPO:-${GITEE_REPO}}"
GITEE_RESULTS_REPO_URL="${GITEE_RESULTS_REPO_URL:-${GITEE_REPO_URL}}"
PUBLISH_GITEE_RESULTS="${PUBLISH_GITEE_RESULTS:-1}"
GITEE_USERNAME="${GITEE_USERNAME:-${GITEE_OWNER}}"
GITEE_WEB_URL="${GITEE_WEB_URL:-https://gitee.com/${GITEE_OWNER}/${GITEE_REPO}}"
GITEE_RESULTS_WEB_URL="${GITEE_RESULTS_WEB_URL:-https://gitee.com/${GITEE_RESULTS_OWNER}/${GITEE_RESULTS_REPO}}"
LOCAL_CI_CONTAINER="${LOCAL_CI_CONTAINER:-triton-anchor-dev}"
LOCAL_CI_WORKSPACE_HOST="${LOCAL_CI_WORKSPACE_HOST:-/root/projects/test/workspace}"
BACKEND_PROFILE="${BACKEND_PROFILE:-sophgo-cmodel}"
RUN_COMPILE_BENCHMARK="${RUN_COMPILE_BENCHMARK:-true}"
RUN_PASS_PROFILE="${RUN_PASS_PROFILE:-true}"
RUN_IR_SERIALIZATION_BENCHMARK="${RUN_IR_SERIALIZATION_BENCHMARK:-true}"
RUN_CODEX_SMOKE="${RUN_CODEX_SMOKE:-false}"
CODEX_SMOKE_BRANCH_REGEX="${CODEX_SMOKE_BRANCH_REGEX:-^ci/push/}"
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_WORKSPACE_ROOT="${CODEX_WORKSPACE_ROOT:-${LOCAL_CI_STATE_DIR%/}/codex-workspaces}"
CODEX_SMOKE_WORKSPACE_ROOT="${CODEX_SMOKE_WORKSPACE_ROOT:-${CODEX_WORKSPACE_ROOT}}"
CODEX_SMOKE_TIMEOUT_SECONDS="${CODEX_SMOKE_TIMEOUT_SECONDS:-300}"
CODEX_SMOKE_REASONING_EFFORT="${CODEX_SMOKE_REASONING_EFFORT:-low}"
RUN_CODEX_AI_CI="${RUN_CODEX_AI_CI:-${RUN_CODEX_SMOKE}}"
CODEX_AI_CI_BRANCH_REGEX="${CODEX_AI_CI_BRANCH_REGEX:-^ci/push/}"
CODEX_AI_CI_WORKSPACE_ROOT="${CODEX_AI_CI_WORKSPACE_ROOT:-${CODEX_WORKSPACE_ROOT}}"
CODEX_AI_CI_TIMEOUT_SECONDS="${CODEX_AI_CI_TIMEOUT_SECONDS:-900}"
CODEX_AI_CI_REASONING_EFFORT="${CODEX_AI_CI_REASONING_EFFORT:-medium}"
export GITEE_TOKEN GITEE_USERNAME GITEE_WEB_URL GITEE_RESULTS_WEB_URL WORKSPACE LOCAL_CI_WORKSPACE_HOST LOCAL_CI_CONFIG LOCAL_CI_CONTAINER

mkdir -p "${LOCAL_CI_STATE_DIR}"
export GIT_TERMINAL_PROMPT=0
if [[ -n "${GITEE_TOKEN}" ]]; then
  gitee_askpass="${LOCAL_CI_STATE_DIR}/gitee-askpass.sh"
  cat > "${gitee_askpass}" <<'SH'
#!/usr/bin/env sh
case "$1" in
  *Username*) printf '%s\n' "${GITEE_USERNAME}" ;;
  *) printf '%s\n' "${GITEE_TOKEN}" ;;
esac
SH
  chmod 700 "${gitee_askpass}"
  export GIT_ASKPASS="${gitee_askpass}"
fi

lock_file="${LOCAL_CI_STATE_DIR}/poll.lock"

exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another local-ci poller is already running: ${lock_file}" >&2
  exit 1
fi

latest_sha() {
  local branch="$1"
  git ls-remote "${GITEE_REPO_URL}" "refs/heads/${branch}" | awk '{print $1}'
}

safe_path_part() {
  local value="$1"
  value="${value//\//_}"
  value="$(printf '%s' "${value}" | tr -c 'A-Za-z0-9._-' '_')"
  value="${value##_}"
  value="${value%%_}"
  printf '%s' "${value:-default}"
}

list_branches() {
  if [[ "${GITEE_POLL_ALL_BRANCHES}" == "1" ]]; then
    git ls-remote --heads "${GITEE_REPO_URL}" |
      awk '{sub(/^refs\/heads\//, "", $2); print $2}'
    return 0
  fi

  printf '%s\n' ${GITEE_BRANCHES}
}

branch_is_enabled() {
  local branch="$1"
  if [[ -z "${branch}" ]]; then
    return 1
  fi
  if [[ "${branch}" == "${GITEE_RESULTS_BRANCH}" ]]; then
    return 1
  fi
  if [[ -n "${GITEE_BRANCH_INCLUDE_REGEX}" && ! "${branch}" =~ ${GITEE_BRANCH_INCLUDE_REGEX} ]]; then
    return 1
  fi
  return 0
}

flaggems_mode_for_branch() {
  local branch="$1"
  case "${branch}" in
    ci/full/*) printf 'full' ;;
    *) printf '%s' "${FLAGGEMS_TEST_MODE:-sample}" ;;
  esac
}

run_codex_smoke_for_run() {
  local sha="$1"
  local run_dir="$2"
  local branch="$3"

  CODEX_BIN="${CODEX_BIN}" \
    CODEX_SMOKE_WORKSPACE_ROOT="${CODEX_SMOKE_WORKSPACE_ROOT}" \
    CODEX_SMOKE_TIMEOUT_SECONDS="${CODEX_SMOKE_TIMEOUT_SECONDS}" \
    CODEX_SMOKE_REASONING_EFFORT="${CODEX_SMOKE_REASONING_EFFORT}" \
    "${LOCAL_CI_RUNNER_DIR}/run_codex_smoke.sh" \
    "${GITEE_REPO_URL}" "${run_dir}" "${sha}" "${branch}"
}

run_codex_ai_ci_for_run() {
  local sha="$1"
  local run_dir="$2"
  local base_sha="$3"
  local branch="$4"

  CODEX_BIN="${CODEX_BIN}" \
    CODEX_AI_CI_TIMEOUT_SECONDS="${CODEX_AI_CI_TIMEOUT_SECONDS}" \
    CODEX_AI_CI_REASONING_EFFORT="${CODEX_AI_CI_REASONING_EFFORT}" \
    CODEX_AI_CI_WORKSPACE_ROOT="${CODEX_AI_CI_WORKSPACE_ROOT}" \
    "${LOCAL_CI_RUNNER_DIR}/run_codex_ai_ci.sh" \
    "${GITEE_REPO_URL}" "${run_dir}" "${sha}" "${base_sha}" "${branch}"
}

publish_result() {
  local sha="$1"
  local status="$2"
  local run_id="$3"
  local run_dir="$4"
  local branch="$5"
  if [[ "${PUBLISH_GITEE_RESULTS}" != "1" ]]; then
    echo "PUBLISH_GITEE_RESULTS is not 1; skip publishing Gitee result branch and commit comment."
    return 0
  fi
  local args=(
    --owner "${GITEE_OWNER}"
    --repo "${GITEE_REPO}"
    --repo-url "${GITEE_REPO_URL}"
    --results-owner "${GITEE_RESULTS_OWNER}"
    --results-repo "${GITEE_RESULTS_REPO}"
    --results-repo-url "${GITEE_RESULTS_REPO_URL}"
    --results-web-url "${GITEE_RESULTS_WEB_URL}"
    --sha "${sha}"
    --source-branch "${branch}"
    --run-id "${run_id}"
    --run-dir "${run_dir}"
    --exit-code "${status}"
    --results-branch "${GITEE_RESULTS_BRANCH}"
    --context "${GITEE_RESULT_CONTEXT}"
  )
  "${LOCAL_CI_RUNNER_DIR}/publish_gitee_result.py" "${args[@]}"
}

stage_runner_scripts() {
  local run_id="$1"
  if [[ ! -d "${LOCAL_CI_SCRIPT_DIR}" ]]; then
    echo "LOCAL_CI_SCRIPT_DIR does not exist: ${LOCAL_CI_SCRIPT_DIR}" >&2
    return 1
  fi

  local staged_dir="${LOCAL_CI_STATE_DIR}/runner/${run_id}"
  rm -rf "${staged_dir}"
  mkdir -p "${staged_dir}"
  cp -a "${LOCAL_CI_SCRIPT_DIR}/." "${staged_dir}/"
  printf '%s' "${staged_dir}"
}

cached_benchmark_exists() {
  local kind="$1"
  local sha="$2"
  local safe_profile
  safe_profile="$(safe_path_part "${BACKEND_PROFILE}")"
  local rel_path="${kind}/by-sha/${sha}/${safe_profile}/latest.json"
  local checkout_dir
  checkout_dir="$(mktemp -d "${LOCAL_CI_STATE_DIR}/baseline-check.XXXXXX")"
  local status=1

  git -C "${checkout_dir}" init -q
  git -C "${checkout_dir}" remote add origin "${GITEE_RESULTS_REPO_URL}"
  if git -C "${checkout_dir}" fetch -q --depth=1 origin \
    "refs/heads/${GITEE_RESULTS_BRANCH}:refs/remotes/origin/${GITEE_RESULTS_BRANCH}"; then
    if git -C "${checkout_dir}" cat-file -e "origin/${GITEE_RESULTS_BRANCH}:${rel_path}" 2>/dev/null; then
      status=0
    fi
  fi

  rm -rf "${checkout_dir}"
  return "${status}"
}

compile_baseline_exists() {
  cached_benchmark_exists "compile-time" "$1"
}

pass_profile_baseline_exists() {
  cached_benchmark_exists "pass-profile" "$1"
}

ir_serialization_baseline_exists() {
  cached_benchmark_exists "ir-serialization" "$1"
}

run_once() {
  local branch="$1"
  local sha
  sha="$(latest_sha "${branch}")"
  if [[ -z "${sha}" ]]; then
    echo "No commit found at ${GITEE_REPO_URL} refs/heads/${branch}" >&2
    return 1
  fi

  local safe_branch
  safe_branch="$(safe_path_part "${branch}")"
  local last_file="${LOCAL_CI_STATE_DIR}/last-processed-${safe_branch}.sha"
  local last=""
  if [[ -f "${last_file}" ]]; then
    last="$(<"${last_file}")"
  fi

  if [[ "${sha}" == "${last}" ]]; then
    echo "No new commit on ${branch}: ${sha}"
    return 0
  fi

  local run_id
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}"
  local run_dir="${LOCAL_CI_STATE_DIR}/runs/${safe_branch}/${run_id}"
  mkdir -p "${run_dir}"

  echo "Detected new commit on ${branch}: ${sha}"
  echo "Run directory: ${run_dir}"

  LOCAL_CI_RUNNER_DIR="$(stage_runner_scripts "${run_id}")"
  export LOCAL_CI_RUNNER_DIR
  echo "Runner script snapshot: ${LOCAL_CI_RUNNER_DIR}"

  local flaggems_test_mode
  flaggems_test_mode="$(flaggems_mode_for_branch "${branch}")"
  echo "FlagGems test mode: ${flaggems_test_mode}"

  local base_branch=""
  local base_sha=""
  if [[ ("${RUN_COMPILE_BENCHMARK}" == "true" || "${RUN_PASS_PROFILE}" == "true" \
    || "${RUN_IR_SERIALIZATION_BENCHMARK}" == "true") \
    && "${branch}" =~ ^ci/pr-([0-9]+)/(.+)$ ]]; then
    base_branch="ci/base/pr-${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    base_sha="$(latest_sha "${base_branch}")"
    if [[ -z "${base_sha}" ]]; then
      echo "No base SHA found for ${branch}; performance comparisons will report a warning." >&2
    else
      local missing_baseline=0
      if [[ "${RUN_COMPILE_BENCHMARK}" == "true" ]]; then
        if compile_baseline_exists "${base_sha}"; then
          echo "Using cached compile-time baseline for ${base_sha}."
        else
          echo "Compile-time baseline missing for ${base_sha}."
          missing_baseline=1
        fi
      fi
      if [[ "${RUN_PASS_PROFILE}" == "true" ]]; then
        if pass_profile_baseline_exists "${base_sha}"; then
          echo "Using cached pass-profile baseline for ${base_sha}."
        else
          echo "Pass-profile baseline missing for ${base_sha}."
          missing_baseline=1
        fi
      fi
      if [[ "${RUN_IR_SERIALIZATION_BENCHMARK}" == "true" ]]; then
        if ir_serialization_baseline_exists "${base_sha}"; then
          echo "Using cached IR serialization baseline for ${base_sha}."
        else
          echo "IR serialization baseline missing for ${base_sha}."
          missing_baseline=1
        fi
      fi
      if [[ "${missing_baseline}" == "1" ]]; then
        local base_run_id
        base_run_id="$(date -u +%Y%m%dT%H%M%SZ)-${base_sha:0:12}-base"
        local base_run_dir="${LOCAL_CI_STATE_DIR}/runs/$(safe_path_part "${base_branch}")/${base_run_id}"
        mkdir -p "${base_run_dir}"
        echo "Running base task once to populate missing performance baseline(s) for ${base_sha}."

        local base_status=0
        set +e
        LOCAL_CI_BASE_SHA="" LOCAL_CI_BASE_REF="" GITEE_BRANCH="${base_branch}" \
          FLAGGEMS_TEST_MODE="${flaggems_test_mode}" \
          "${LOCAL_CI_RUNNER_DIR}/run_in_container.sh" "${base_sha}" "${base_branch}" 2>&1 |
          tee "${base_run_dir}/local-ci.log"
        base_status=${PIPESTATUS[0]}
        set -e
        echo "{\"sha\":\"${base_sha}\",\"status\":${base_status},\"run_dir\":\"${base_run_dir}\"}" \
          > "${base_run_dir}/result.json"
        publish_result "${base_sha}" "${base_status}" "${base_run_id}" "${base_run_dir}" "${base_branch}" || true
        if [[ ${base_status} -ne 0 ]]; then
          echo "Base task failed; continuing candidate task with a missing-baseline warning." >&2
        fi
      fi
    fi
  fi

  local status=0
  set +e
  LOCAL_CI_BASE_SHA="${base_sha}" LOCAL_CI_BASE_REF="${base_branch}" GITEE_BRANCH="${branch}" \
    FLAGGEMS_TEST_MODE="${flaggems_test_mode}" \
    "${LOCAL_CI_RUNNER_DIR}/run_in_container.sh" "${sha}" "${branch}" 2>&1 |
    tee "${run_dir}/local-ci.log"
  status=${PIPESTATUS[0]}
  set -e

  local codex_smoke_status="skipped"
  if [[ "${RUN_CODEX_SMOKE}" == "true" \
    && (-z "${CODEX_SMOKE_BRANCH_REGEX}" || "${branch}" =~ ${CODEX_SMOKE_BRANCH_REGEX}) ]]; then
    echo "Running non-blocking Codex smoke for ${sha}." | tee -a "${run_dir}/local-ci.log"
    local codex_smoke_exit=0
    set +e
    run_codex_smoke_for_run "${sha}" "${run_dir}" "${branch}" 2>&1 |
      tee -a "${run_dir}/local-ci.log"
    codex_smoke_exit=${PIPESTATUS[0]}
    set -e
    if [[ ${codex_smoke_exit} -eq 0 ]]; then
      codex_smoke_status="pass"
    else
      codex_smoke_status="fail"
      echo "Codex smoke failed but does not change the deterministic local-ci result." |
        tee -a "${run_dir}/local-ci.log"
    fi
  else
    echo "Codex smoke skipped for ${branch}." | tee -a "${run_dir}/local-ci.log"
  fi

  local codex_ai_base_sha="${base_sha}"
  if [[ -z "${codex_ai_base_sha}" && -n "${last}" ]]; then
    codex_ai_base_sha="${last}"
  fi

  local codex_ai_ci_status="skipped"
  local codex_ai_ci_verdict="NOT_RUN"
  if [[ "${RUN_CODEX_AI_CI}" == "true" \
    && (-z "${CODEX_AI_CI_BRANCH_REGEX}" || "${branch}" =~ ${CODEX_AI_CI_BRANCH_REGEX}) ]]; then
    codex_ai_ci_verdict="UNKNOWN"
    echo "Running non-blocking Codex AI diff analysis for ${sha}." | tee -a "${run_dir}/local-ci.log"
    local codex_ai_ci_exit=0
    set +e
    run_codex_ai_ci_for_run "${sha}" "${run_dir}" "${codex_ai_base_sha}" "${branch}" 2>&1 |
      tee -a "${run_dir}/local-ci.log"
    codex_ai_ci_exit=${PIPESTATUS[0]}
    set -e
    local codex_ai_summary="${run_dir}/codex-ai-ci-summary.txt"
    if [[ -f "${codex_ai_summary}" ]]; then
      local parsed_codex_ai_verdict
      parsed_codex_ai_verdict="$(awk -F ': ' '$1 == "report_verdict" { print $2; exit }' "${codex_ai_summary}")"
      case "${parsed_codex_ai_verdict}" in
        PASS | WARNING | FAIL) codex_ai_ci_verdict="${parsed_codex_ai_verdict}" ;;
      esac
    fi
    if [[ ${codex_ai_ci_exit} -eq 0 ]]; then
      codex_ai_ci_status="pass"
    else
      codex_ai_ci_status="fail"
      echo "Codex AI CI failed but does not change the deterministic local-ci result." |
        tee -a "${run_dir}/local-ci.log"
    fi
  else
    echo "Codex AI CI skipped for ${branch}." | tee -a "${run_dir}/local-ci.log"
  fi

  echo "{\"sha\":\"${sha}\",\"status\":${status},\"codex_smoke_status\":\"${codex_smoke_status}\",\"codex_ai_ci_status\":\"${codex_ai_ci_status}\",\"codex_ai_ci_verdict\":\"${codex_ai_ci_verdict}\",\"run_dir\":\"${run_dir}\"}" \
    > "${run_dir}/result.json"

  local publish_status=0
  set +e
  publish_result "${sha}" "${status}" "${run_id}" "${run_dir}" "${branch}"
  publish_status=$?
  set -e

  if [[ ${publish_status} -eq 0 ]]; then
    echo "${sha}" > "${last_file}"
    if [[ ${status} -eq 0 ]]; then
      echo "local-ci passed and result was published; marked ${sha} processed."
    else
      echo "local-ci failed and result was published; marked ${sha} processed."
    fi
  else
    echo "local-ci result publish failed; ${sha} was not marked processed and will be retried." >&2
  fi

  if [[ ${status} -ne 0 ]]; then
    return "${status}"
  fi
  return "${publish_status}"
}

run_all_once() {
  local status=0
  local branch
  while IFS= read -r branch; do
    if ! branch_is_enabled "${branch}"; then
      continue
    fi
    run_once "${branch}" || status=1
  done < <(list_branches | awk 'NF' | sort -u)
  return "${status}"
}

if [[ "${1:-}" == "--once" ]]; then
  LOCAL_CI_ONCE="1"
fi

while true; do
  loop_status=0
  run_all_once || loop_status=$?
  if [[ "${LOCAL_CI_ONCE}" == "1" ]]; then
    exit "${loop_status}"
  fi
  sleep "${LOCAL_CI_POLL_INTERVAL}"
done
