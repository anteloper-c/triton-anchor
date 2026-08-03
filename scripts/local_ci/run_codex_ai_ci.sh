#!/usr/bin/env bash
set -uo pipefail

usage="run_codex_ai_ci.sh <repo-url> <output-dir> <target-sha> <base-sha> <base-ref> <branch> [local-ci-status] [task-metadata-file]"
repo_url="${1:?usage: ${usage}}"
output_dir="${2:?usage: ${usage}}"
target_sha="${3:?usage: ${usage}}"
requested_base_sha="${4:-}"
requested_base_ref="${5:-}"
branch="${6:?usage: ${usage}}"
local_ci_status="${7:-0}"
task_metadata_file="${8:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_AI_CI_HOME="${CODEX_AI_CI_HOME:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CODEX_AI_CI_TIMEOUT_SECONDS="${CODEX_AI_CI_TIMEOUT_SECONDS:-1800}"
CODEX_AI_CI_REASONING_EFFORT="${CODEX_AI_CI_REASONING_EFFORT:-medium}"
CODEX_AI_CI_WORKSPACE_ROOT="${CODEX_AI_CI_WORKSPACE_ROOT:-${TMPDIR:-/tmp}/triton-anchor-codex-ai}"
CODEX_AI_CI_MIN_GENERATED_TEST_CASES="${CODEX_AI_CI_MIN_GENERATED_TEST_CASES:-1}"
CODEX_AI_CI_MAX_GENERATED_TEST_CASES="${CODEX_AI_CI_MAX_GENERATED_TEST_CASES:-3}"
CODEX_AI_CI_MAX_GENERATED_TEST_FILES="${CODEX_AI_CI_MAX_GENERATED_TEST_FILES:-2}"
CODEX_AI_CI_MAX_TEST_COMMANDS="${CODEX_AI_CI_MAX_TEST_COMMANDS:-4}"
CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS="${CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS:-600}"
CODEX_AI_CI_TEST_BUDGET_SECONDS="${CODEX_AI_CI_TEST_BUDGET_SECONDS:-1200}"
CODEX_AI_CI_REPORT_RESERVE_SECONDS="${CODEX_AI_CI_REPORT_RESERVE_SECONDS:-300}"
LOCAL_CI_CONTAINER="${LOCAL_CI_CONTAINER:-anchor-sophgo-ci}"
PYTHON_VENV_ACTIVATE="${PYTHON_VENV_ACTIVATE:-/opt/venv/bin/activate}"
SOURCE_ENVSETUP="${SOURCE_ENVSETUP:-1}"
ANCHOR_DIR="${ANCHOR_DIR:-/workspace/triton-anchor}"
BACKEND_PATH="${BACKEND_PATH:-/workspace/triton-sophgo-backend}"
BACKEND_ENVSETUP="${BACKEND_ENVSETUP:-envsetup.sh}"
BACKEND_ENVSETUP_ARGS="${BACKEND_ENVSETUP_ARGS:-PIO_CMODEL}"

container_codex_bin="/usr/local/bin/codex"
container_codex_home="/root/.codex"
container_workspace_root="/codex-workspace"
container_checkout_dir="${container_workspace_root}/checkout"
container_input_dir="${container_workspace_root}/input"
container_report_json_path="${container_workspace_root}/codex-ai-report.json"
container_schema_path="${container_workspace_root}/codex-ai-report.schema.json"
container_local_ci_log="${container_input_dir}/local-ci.log"

log_path="${output_dir}/codex-ai-ci.log"
report_json_path="${output_dir}/codex-ai-report.json"
report_path="${output_dir}/codex-ai-report.md"
comment_path="${output_dir}/codex-ai-comment.md"
summary_path="${output_dir}/codex-ai-ci-summary.txt"
workspace_status_path="${output_dir}/codex-workspace-status.txt"
workspace_patch_path="${output_dir}/codex-workspace.patch"
generated_files_path="${output_dir}/codex-generated-files.tar.gz"
schema_path="${SCRIPT_DIR}/codex_ai_report.schema.json"
renderer_path="${SCRIPT_DIR}/render_codex_ai_report.py"
checkout_helper="${SCRIPT_DIR}/prepare_codex_checkout.sh"
credentials_validator="${SCRIPT_DIR}/validate_codex_ai_credentials.py"
task_metadata_validator="${SCRIPT_DIR}/validate_task_metadata.py"
task_metadata_output_path="${output_dir}/task-metadata.json"
prompt_dir="${SCRIPT_DIR}/prompts"
common_prompt_template="${prompt_dir}/codex_ai_common.md"
full_prompt_template="${prompt_dir}/codex_ai_full.md"
analysis_only_prompt_template="${prompt_dir}/codex_ai_analysis_only.md"

status="fail"
exit_code=1
actual_sha=""
base_sha=""
base_source=""
diff_mode="unresolved"
diff_command=""
diff_revisions=()
changed_file_count=0
failure_reason=""
marker_found="false"
report_format_valid="false"
report_verdict="UNKNOWN"
test_execution_status="UNKNOWN"
generated_test_file_count="UNKNOWN"
test_command_count="UNKNOWN"
max_test_command_duration_seconds="UNKNOWN"
total_test_command_duration_seconds="UNKNOWN"
test_generation_expected="false"
constraint_status="warning"
constraint_reason="尚未获得可校验的测试执行信息。"
turn_completed="false"
command_executed="false"
workspace_dirty="false"
workspace_dir=""
workspace_parent=""
artifact_dir=""
host_codex_bin=""
credential_integrity_status="not_checked"
credential_integrity_reason="尚未校验独立凭据文件。"
credential_hashes_initialized="false"
config_sha256_before=""
auth_sha256_before=""
change_request_context_status="not_applicable"
change_request_context_reason="当前任务不是 PR，功能声明上下文不适用。"
change_request_context_pr_number=""
change_request_context_json=""
ephemeral_container=""
ephemeral_image=""
analysis_mode="full"
start_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_seconds="${SECONDS}"
if [[ "${local_ci_status}" != "0" ]]; then
  analysis_mode="analysis_only"
  constraint_status="not_applicable"
  constraint_reason="只分析模式不执行测试数量和耗时约束校验。"
fi
if [[ "${branch}" =~ ^ci/pr-[0-9]+/.+ ]]; then
  change_request_context_status="not_checked"
  change_request_context_reason="尚未校验 PR 功能声明元数据。"
fi

cleanup() {
  if [[ -n "${ephemeral_container}" ]]; then
    case "${ephemeral_container}" in
      anchor-codex-ai-*)
        docker rm -f "${ephemeral_container}" >/dev/null 2>&1 || true
        ;;
    esac
  fi
  if [[ -n "${ephemeral_image}" ]]; then
    case "${ephemeral_image}" in
      triton-anchor-codex-ai-snapshot:*)
        docker image rm -f "${ephemeral_image}" >/dev/null 2>&1 || true
        ;;
    esac
  fi
  if [[ -n "${workspace_parent}" && -d "${workspace_parent}" ]]; then
    rm -rf -- "${workspace_parent}"
  fi
}
trap cleanup EXIT

write_summary() {
  local duration_seconds="$((SECONDS - start_seconds))"
  {
    echo "schema: triton-anchor-codex-ai-ci/v3"
    echo "status: ${status}"
    echo "exit_code: ${exit_code}"
    echo "target_sha: ${target_sha}"
    echo "actual_sha: ${actual_sha}"
    echo "requested_base_sha: ${requested_base_sha}"
    echo "requested_base_ref: ${requested_base_ref}"
    echo "base_sha: ${base_sha}"
    echo "base_source: ${base_source}"
    echo "diff_mode: ${diff_mode}"
    echo "branch: ${branch}"
    echo "repo_source: gitee"
    echo "local_ci_status: ${local_ci_status}"
    echo "analysis_mode: ${analysis_mode}"
    echo "execution_mode: ephemeral_container"
    echo "source_container: ${LOCAL_CI_CONTAINER}"
    echo "ephemeral_container: ${ephemeral_container}"
    echo "snapshot_image: ${ephemeral_image}"
    echo "workspace_reuse: read_only"
    echo "workspace_dir: ${workspace_dir}"
    echo "container_workspace_dir: ${container_checkout_dir}"
    echo "artifact_dir: ${artifact_dir}"
    echo "output_dir: ${output_dir}"
    echo "changed_file_count: ${changed_file_count}"
    echo "started_at: ${start_time}"
    echo "duration_seconds: ${duration_seconds}"
    echo "timeout_seconds: ${CODEX_AI_CI_TIMEOUT_SECONDS}"
    echo "reasoning_effort: ${CODEX_AI_CI_REASONING_EFFORT}"
    echo "min_generated_test_cases: ${CODEX_AI_CI_MIN_GENERATED_TEST_CASES}"
    echo "max_generated_test_cases: ${CODEX_AI_CI_MAX_GENERATED_TEST_CASES}"
    echo "max_generated_test_files: ${CODEX_AI_CI_MAX_GENERATED_TEST_FILES}"
    echo "max_test_commands: ${CODEX_AI_CI_MAX_TEST_COMMANDS}"
    echo "recommended_command_timeout_seconds: ${CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS}"
    echo "test_budget_seconds: ${CODEX_AI_CI_TEST_BUDGET_SECONDS}"
    echo "report_reserve_seconds: ${CODEX_AI_CI_REPORT_RESERVE_SECONDS}"
    echo "marker_found: ${marker_found}"
    echo "report_format_valid: ${report_format_valid}"
    echo "report_verdict: ${report_verdict}"
    echo "test_execution_status: ${test_execution_status}"
    echo "generated_test_file_count: ${generated_test_file_count}"
    echo "test_command_count: ${test_command_count}"
    echo "max_test_command_duration_seconds: ${max_test_command_duration_seconds}"
    echo "total_test_command_duration_seconds: ${total_test_command_duration_seconds}"
    echo "test_generation_expected: ${test_generation_expected}"
    echo "constraint_status: ${constraint_status}"
    echo "constraint_reason: ${constraint_reason}"
    echo "turn_completed: ${turn_completed}"
    echo "command_executed: ${command_executed}"
    echo "workspace_dirty: ${workspace_dirty}"
    echo "credential_integrity_status: ${credential_integrity_status}"
    echo "credential_integrity_reason: ${credential_integrity_reason}"
    echo "change_request_context_status: ${change_request_context_status}"
    echo "change_request_context_reason: ${change_request_context_reason}"
    echo "change_request_context_pr_number: ${change_request_context_pr_number}"
    echo "failure_reason: ${failure_reason}"
  } > "${summary_path}"
}

write_failure_report() {
  cat > "${report_json_path}" <<'JSON'
{
  "verdict": "WARNING",
  "summary": "Codex AI CI 未完成，未生成可信的结构化审查结论。",
  "findings": [
    {
      "id": "AI-001",
      "severity": "MEDIUM",
      "category": "other",
      "file": "不可用",
      "line": "不可用",
      "title": "Codex AI CI 分析未完成",
      "evidence": "执行环境、超时或输出格式校验失败，具体原因请查看本次任务的摘要和原始日志。",
      "impact": "本次代码差异没有获得完整可靠的 AI 审查结论。",
      "fix_direction": "排查 Codex AI CI 摘要和日志中的失败原因后重新执行。"
    }
  ],
  "suggested_tests": [],
  "residual_risks": [
    "当前代码差异仍需人工检查，或在修复执行环境后重新运行 Codex AI CI。"
  ],
  "test_execution": {
    "status": "infrastructure_failure",
    "summary": "Codex AI CI 未完成，因此没有可信的测试执行结论。",
    "generated_test_files": [],
    "commands": []
  },
  "completion_marker": "CODEX_AI_CI_COMPLETE"
}
JSON
  {
    echo "# Codex AI CI 报告"
    echo
    echo "## 元数据"
    echo
    echo "| 字段 | 值 |"
    echo "| --- | --- |"
    echo "| 报告格式 | \`triton-anchor-codex-ai-report/v1\` |"
    echo "| 分支 | \`${branch}\` |"
    echo "| 请求的基础提交 | \`${requested_base_sha:-不可用}\` |"
    echo "| 实际审查起点 | \`${base_sha:-不可用}\` |"
    echo "| 差异模式 | \`${diff_mode}\` |"
    echo "| 目标提交 | \`${target_sha}\` |"
    echo "| 变更文件数 | ${changed_file_count} |"
    echo "| 生成时间（UTC） | \`${start_time}\` |"
    echo
    echo "## 结论"
    echo
    echo "**失败**"
    echo
    echo "## 摘要"
    echo
    echo "Codex AI CI 未完成：${failure_reason}"
    echo
    echo "## 关键问题"
    echo
    echo "分析未完成，无法给出可靠的问题结论。"
    echo
    echo "## 建议测试"
    echo
    echo "无。"
    echo
    echo "## 测试执行"
    echo
    echo "- 状态：基础设施失败"
    echo "- 摘要：Codex AI CI 未完成，未获得可信的测试结果。"
    echo
    echo "## 剩余风险"
    echo
    echo "- 本次 AI 审查未完成，当前代码差异仍需人工检查。"
    echo
    echo "## 执行标记"
    echo
    echo "CODEX_AI_CI_FAILED"
  } > "${report_path}"
  {
    echo "## Codex AI 关键问题概述"
    echo
    echo "Codex 分析未完成：${failure_reason:-未知原因}。请查看完整日志。"
  } > "${comment_path}"
}

append_credential_integrity_warning() {
  if [[ "${credential_integrity_status}" != "warning" ]]; then
    return 0
  fi
  {
    echo
    echo "## 凭据完整性"
    echo
    echo "- 状态：警告"
    echo "- 说明：${credential_integrity_reason}"
  } >> "${report_path}"
  {
    echo
    echo "### Codex AI CI 凭据完整性警告"
    echo
    echo "${credential_integrity_reason}"
  } >> "${comment_path}"
  echo "Codex AI CI 凭据完整性警告：${credential_integrity_reason}" >> "${log_path}"
}

append_change_request_context_warning() {
  case "${change_request_context_status}" in
    missing | invalid) ;;
    *) return 0 ;;
  esac
  {
    echo
    echo "## PR 功能声明上下文"
    echo
    echo "- 状态：警告"
    echo "- 说明：${change_request_context_reason}"
  } >> "${report_path}"
  {
    echo
    echo "### PR 功能声明上下文警告"
    echo
    echo "${change_request_context_reason}"
  } >> "${comment_path}"
  echo "PR 功能声明上下文警告：${change_request_context_reason}" >> "${log_path}"
}

fail_ai_ci() {
  failure_reason="$1"
  verify_credential_integrity
  write_failure_report
  append_change_request_context_warning
  append_credential_integrity_warning
  echo "Codex AI CI 失败：${failure_reason}" >> "${log_path}"
  write_summary
  echo "Codex AI CI：失败（${failure_reason}）"
  exit 1
}

resolve_codex_binary() {
  if [[ "${CODEX_BIN}" == */* ]]; then
    host_codex_bin="${CODEX_BIN}"
  else
    host_codex_bin="$(command -v "${CODEX_BIN}" 2>/dev/null || true)"
  fi
  if [[ -z "${host_codex_bin}" || ! -x "${host_codex_bin}" ]]; then
    fail_ai_ci "宿主机上找不到可执行的 Codex CLI：${CODEX_BIN}"
  fi
}

credential_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

capture_credential_hashes() {
  config_sha256_before="$(credential_sha256 "${CODEX_AI_CI_HOME}/config.toml")"
  auth_sha256_before="$(credential_sha256 "${CODEX_AI_CI_HOME}/auth.json")"
  credential_hashes_initialized="true"
  credential_integrity_status="pass"
  credential_integrity_reason="独立凭据文件在任务执行前后保持不变。"
}

verify_credential_integrity() {
  local config_sha256_after
  local auth_sha256_after
  if [[ "${credential_hashes_initialized}" != "true" ]]; then
    return 0
  fi
  if [[ ! -f "${CODEX_AI_CI_HOME}/config.toml" || ! -f "${CODEX_AI_CI_HOME}/auth.json" ]]; then
    credential_integrity_status="warning"
    credential_integrity_reason="任务执行期间独立凭据文件被删除或替换；runner 未自动恢复文件。"
    return 0
  fi
  config_sha256_after="$(credential_sha256 "${CODEX_AI_CI_HOME}/config.toml" 2>/dev/null || true)"
  auth_sha256_after="$(credential_sha256 "${CODEX_AI_CI_HOME}/auth.json" 2>/dev/null || true)"
  if [[ -z "${config_sha256_after}" || -z "${auth_sha256_after}" ]]; then
    credential_integrity_status="warning"
    credential_integrity_reason="任务结束时无法重新计算独立凭据文件哈希；runner 未修改或恢复文件。"
  elif [[ "${config_sha256_after}" != "${config_sha256_before}" || "${auth_sha256_after}" != "${auth_sha256_before}" ]]; then
    credential_integrity_status="warning"
    credential_integrity_reason="任务执行期间独立凭据文件内容发生变化；runner 未自动恢复文件，请人工检查。"
  else
    credential_integrity_status="pass"
    credential_integrity_reason="独立凭据文件在任务执行前后保持不变。"
  fi
}

validate_positive_integer() {
  local name="$1"
  local value="$2"
  case "${value}" in
    "" | *[!0-9]*) fail_ai_ci "${name} 必须是正整数" ;;
  esac
  if (( 10#${value} <= 0 )); then
    fail_ai_ci "${name} 必须大于 0"
  fi
}

render_prompt_template() {
  local template_path="$1"
  shift
  "${PYTHON_BIN}" - "${template_path}" "$@" <<'PY'
import sys
from pathlib import Path
from string import Template

template_path = Path(sys.argv[1])
items = sys.argv[2:]
if len(items) % 2:
    print("提示词模板变量必须按名称和值成对传入", file=sys.stderr)
    raise SystemExit(2)
values = dict(zip(items[0::2], items[1::2]))
try:
    rendered = Template(template_path.read_text(encoding="utf-8")).substitute(values)
except (OSError, UnicodeError, KeyError, ValueError) as exc:
    print(f"无法渲染提示词模板 {template_path}: {exc}", file=sys.stderr)
    raise SystemExit(2)
sys.stdout.write(rendered)
PY
}

build_unavailable_change_request_context() {
  "${PYTHON_BIN}" - "${change_request_context_status}" \
    "${change_request_context_reason}" <<'PY'
import json
import sys

print(
    json.dumps(
        {"status": sys.argv[1], "reason": sys.argv[2]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
)
PY
}

load_change_request_context() {
  if [[ ! "${branch}" =~ ^ci/pr-[0-9]+/.+ ]]; then
    change_request_context_status="not_applicable"
    change_request_context_reason="当前任务不是 PR，功能声明上下文不适用。"
    change_request_context_json="$(build_unavailable_change_request_context)" || \
      fail_ai_ci "无法生成非 PR 功能声明上下文"
    return 0
  fi

  if [[ -z "${task_metadata_file}" || ! -f "${task_metadata_file}" ]]; then
    change_request_context_status="missing"
    change_request_context_reason="未取得与当前 PR head SHA 匹配的功能声明元数据；继续依据代码差异和测试证据分析。"
    change_request_context_json="$(build_unavailable_change_request_context)" || \
      fail_ai_ci "无法生成 PR 元数据缺失上下文"
    echo "${change_request_context_reason}" >> "${log_path}"
    return 0
  fi

  if [[ "${task_metadata_file}" != "${task_metadata_output_path}" ]]; then
    rm -f -- "${task_metadata_output_path}"
  fi
  local validation_message=""
  if ! validation_message="$(
    "${PYTHON_BIN}" "${task_metadata_validator}" \
      --input "${task_metadata_file}" \
      --output "${task_metadata_output_path}" \
      --task-ref "${branch}" \
      --target-sha "${target_sha}" 2>&1
  )"; then
    rm -f -- "${task_metadata_output_path}"
    validation_message="${validation_message//$'\n'/ }"
    change_request_context_status="invalid"
    change_request_context_reason="PR 功能声明元数据校验失败；继续依据代码差异和测试证据分析。${validation_message:+ ${validation_message}}"
    change_request_context_json="$(build_unavailable_change_request_context)" || \
      fail_ai_ci "无法生成 PR 元数据无效上下文"
    echo "${change_request_context_reason}" >> "${log_path}"
    return 0
  fi

  if [[ -n "${validation_message}" ]]; then
    printf '%s\n' "${validation_message}" >> "${log_path}"
  fi
  local context_parts=()
  mapfile -t context_parts < <(
    "${PYTHON_BIN}" - "${task_metadata_output_path}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    metadata = json.load(stream)
context = {
    "status": "available",
    "pr_number": metadata["pr_number"],
    "title": metadata["title"],
    "description": metadata["description"],
    "captured_at": metadata["captured_at"],
    "title_truncated": metadata["title_truncated"],
    "description_truncated": metadata["description_truncated"],
}
print(metadata["pr_number"])
print(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
PY
  )
  if [[ "${#context_parts[@]}" -ne 2 || -z "${context_parts[1]}" ]]; then
    rm -f -- "${task_metadata_output_path}"
    change_request_context_status="invalid"
    change_request_context_reason="规范化后的 PR 功能声明元数据无法读取；继续依据代码差异和测试证据分析。"
    change_request_context_json="$(build_unavailable_change_request_context)" || \
      fail_ai_ci "无法生成 PR 元数据读取失败上下文"
    echo "${change_request_context_reason}" >> "${log_path}"
    return 0
  fi

  change_request_context_status="available"
  change_request_context_reason="已校验并载入与当前 PR head SHA 匹配的功能声明元数据。"
  change_request_context_pr_number="${context_parts[0]}"
  change_request_context_json="${context_parts[1]}"
}

validate_prerequisites() {
  local integer_name
  local prompt_template
  local integer_names=(
    CODEX_AI_CI_TIMEOUT_SECONDS
    CODEX_AI_CI_MIN_GENERATED_TEST_CASES
    CODEX_AI_CI_MAX_GENERATED_TEST_CASES
    CODEX_AI_CI_MAX_GENERATED_TEST_FILES
    CODEX_AI_CI_MAX_TEST_COMMANDS
    CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS
    CODEX_AI_CI_TEST_BUDGET_SECONDS
    CODEX_AI_CI_REPORT_RESERVE_SECONDS
  )
  for integer_name in "${integer_names[@]}"; do
    validate_positive_integer "${integer_name}" "${!integer_name}"
  done

  if ((
    10#${CODEX_AI_CI_MIN_GENERATED_TEST_CASES} >
      10#${CODEX_AI_CI_MAX_GENERATED_TEST_CASES}
  )); then
    fail_ai_ci "生成测试用例下限不能大于上限"
  fi

  case "${local_ci_status}" in
    "" | *[!0-9]*) fail_ai_ci "Local CI 状态必须是非负整数" ;;
  esac
  case "${LOCAL_CI_CONTAINER}" in
    "" | *[!A-Za-z0-9_.-]*) fail_ai_ci "Local CI 容器名称无效：${LOCAL_CI_CONTAINER}" ;;
  esac

  if ! command -v timeout >/dev/null 2>&1; then
    fail_ai_ci "宿主机缺少 timeout 命令"
  fi
  if ! command -v docker >/dev/null 2>&1; then
    fail_ai_ci "宿主机缺少 docker 命令"
  fi
  if ! command -v git >/dev/null 2>&1; then
    fail_ai_ci "宿主机缺少 git 命令"
  fi
  if ! command -v sha256sum >/dev/null 2>&1; then
    fail_ai_ci "宿主机缺少 sha256sum 命令"
  fi
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    fail_ai_ci "宿主机找不到 Python：${PYTHON_BIN}"
  fi
  if [[ ! -r "${schema_path}" ]]; then
    fail_ai_ci "报告 schema 不可读：${schema_path}"
  fi
  if [[ ! -r "${renderer_path}" ]]; then
    fail_ai_ci "报告渲染器不可读：${renderer_path}"
  fi
  if [[ ! -x "${checkout_helper}" ]]; then
    fail_ai_ci "checkout helper 不可执行：${checkout_helper}"
  fi
  if [[ ! -r "${credentials_validator}" ]]; then
    fail_ai_ci "独立凭据校验器不可读：${credentials_validator}"
  fi
  if [[ ! -r "${task_metadata_validator}" ]]; then
    fail_ai_ci "PR 功能声明元数据校验器不可读：${task_metadata_validator}"
  fi
  for prompt_template in \
    "${common_prompt_template}" \
    "${full_prompt_template}" \
    "${analysis_only_prompt_template}"; do
    [[ -r "${prompt_template}" ]] || \
      fail_ai_ci "Codex 提示词模板不可读：${prompt_template}"
  done

  resolve_codex_binary
  if [[ -z "${CODEX_AI_CI_HOME}" ]]; then
    fail_ai_ci "必须设置独立的 CODEX_AI_CI_HOME"
  fi
  local credential_validation_output
  if ! credential_validation_output="$(
    "${PYTHON_BIN}" "${credentials_validator}" \
      --codex-home "${CODEX_AI_CI_HOME}" \
      --personal-codex-home "${HOME}/.codex" \
      --quiet 2>&1
  )"; then
    [[ -n "${credential_validation_output}" ]] && \
      echo "${credential_validation_output}" >> "${log_path}"
    fail_ai_ci "${credential_validation_output:-Codex AI CI 独立凭据校验失败}"
  fi
  if [[ -n "${credential_validation_output}" ]]; then
    echo "${credential_validation_output}" >> "${log_path}"
  fi
  capture_credential_hashes

  if [[ "$(docker inspect --format '{{.State.Running}}' "${LOCAL_CI_CONTAINER}" 2>> "${log_path}" || true)" != "true" ]]; then
    fail_ai_ci "Local CI 容器未运行：${LOCAL_CI_CONTAINER}"
  fi
  if docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
    "${LOCAL_CI_CONTAINER}" 2>> "${log_path}" | grep -Fxq '/var/run/docker.sock'; then
    fail_ai_ci "Local CI 容器挂载了 Docker socket，拒绝将其传递给 Codex 容器"
  fi
}

discover_artifact_dir() {
  if [[ ! -f "${output_dir}/local-ci.log" ]]; then
    return 0
  fi
  artifact_dir="$(
    sed -n 's/.*Artifacts are in \([^[:space:]]*\).*/\1/p' \
      "${output_dir}/local-ci.log" | tail -n 1
  )"
}

diff_requires_generated_tests() {
  local changed_path
  local diff_args=(
    -C "${workspace_dir}"
    diff
    --name-only
    --diff-filter=ACDMRTUXB
    --find-renames
    "${diff_revisions[@]}"
  )
  while IFS= read -r changed_path; do
    [[ -z "${changed_path}" ]] && continue
    case "${changed_path}" in
      docs/* | *.md | *.markdown | *.rst)
        ;;
      README | README.* | LICENSE | LICENSE.* | NOTICE | NOTICE.*)
        ;;
      ROADMAP.md | SECURITY.md)
        ;;
      *)
        return 0
        ;;
    esac
  done < <(git "${diff_args[@]}")
  return 1
}

create_ephemeral_container() {
  local resource_key
  local workspace_rw
  resource_key="$(date -u +%Y%m%dT%H%M%SZ)-${target_sha:0:12}-$$"
  resource_key="${resource_key,,}"
  ephemeral_container="anchor-codex-ai-${resource_key}"
  ephemeral_image="triton-anchor-codex-ai-snapshot:${resource_key}"

  echo "正在从 ${LOCAL_CI_CONTAINER} 创建本次任务的临时镜像 ${ephemeral_image}。" >> "${log_path}"
  if ! docker commit "${LOCAL_CI_CONTAINER}" "${ephemeral_image}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法从本次 Local CI 容器创建环境快照"
  fi

  if ! docker run -dit \
    --name "${ephemeral_container}" \
    --hostname "${ephemeral_container}" \
    --label "triton-anchor.role=codex-ai" \
    --label "triton-anchor.target-sha=${target_sha}" \
    --volumes-from "${LOCAL_CI_CONTAINER}:ro" \
    --entrypoint /bin/bash \
    "${ephemeral_image}" \
    -lc 'trap : TERM INT; while :; do sleep 3600; done' \
    >> "${log_path}" 2>&1; then
    fail_ai_ci "无法启动本次任务的临时 Codex 容器"
  fi

  if [[ "$(docker inspect --format '{{.State.Running}}' "${ephemeral_container}" 2>> "${log_path}" || true)" != "true" ]]; then
    fail_ai_ci "临时 Codex 容器启动后未保持运行"
  fi
  if docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
    "${ephemeral_container}" 2>> "${log_path}" | grep -Fxq '/var/run/docker.sock'; then
    fail_ai_ci "临时 Codex 容器意外挂载了 Docker socket"
  fi
  workspace_rw="$(
    docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{println .RW}}{{end}}{{end}}' \
      "${ephemeral_container}" 2>> "${log_path}" || true
  )"
  if [[ "${workspace_rw}" != "false" ]]; then
    fail_ai_ci "临时 Codex 容器没有以只读方式复用 /workspace"
  fi

  if ! docker exec --user 0 "${ephemeral_container}" \
    mkdir -p \
      "${container_codex_home}" \
      "$(dirname "${container_codex_bin}")" \
      "${container_checkout_dir}" \
      "${container_input_dir}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法在临时容器中创建 Codex 工作目录"
  fi

  if ! docker cp "${host_codex_bin}" \
    "${ephemeral_container}:${container_codex_bin}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法把 Codex CLI 复制到临时容器"
  fi
  if ! docker exec --user 0 "${ephemeral_container}" \
    chmod +x "${container_codex_bin}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法设置容器内 Codex CLI 的执行权限"
  fi
  for config_file in config.toml auth.json; do
    if ! docker cp "${CODEX_AI_CI_HOME}/${config_file}" \
      "${ephemeral_container}:${container_codex_home}/${config_file}" \
      >> "${log_path}" 2>&1; then
      fail_ai_ci "无法把 ${config_file} 复制到临时容器"
    fi
  done

  if ! docker cp "${workspace_dir}/." \
    "${ephemeral_container}:${container_checkout_dir}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法把经过验证的 checkout 复制到临时容器"
  fi
  if ! docker cp "${schema_path}" \
    "${ephemeral_container}:${container_schema_path}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法把报告 schema 复制到临时容器"
  fi
  if [[ -f "${output_dir}/local-ci.log" ]]; then
    if ! docker cp "${output_dir}/local-ci.log" \
      "${ephemeral_container}:${container_local_ci_log}" >> "${log_path}" 2>&1; then
      fail_ai_ci "无法把 Local CI 日志复制到临时容器"
    fi
  fi

  if ! docker exec --user 0 "${ephemeral_container}" \
    chown -R 0:0 \
      "${container_codex_home}" \
      "${container_workspace_root}" \
      "${container_codex_bin}" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法修正临时容器内 Codex 文件的所有权"
  fi
  if ! docker exec --user 0 "${ephemeral_container}" \
    chmod 600 \
      "${container_codex_home}/config.toml" \
      "${container_codex_home}/auth.json" >> "${log_path}" 2>&1; then
    fail_ai_ci "无法收紧临时容器内 Codex 配置文件权限"
  fi

  local copied_sha
  copied_sha="$(
    docker exec --user 0 "${ephemeral_container}" \
      git -C "${container_checkout_dir}" rev-parse HEAD 2>> "${log_path}" || true
  )"
  if [[ "${copied_sha}" != "${target_sha}" ]]; then
    fail_ai_ci "容器内 checkout 的 SHA 与目标 SHA 不一致"
  fi
  if ! docker exec --user 0 "${ephemeral_container}" \
    "${container_codex_bin}" --version >> "${log_path}" 2>&1; then
    fail_ai_ci "Codex CLI 无法在临时容器中启动"
  fi
}


collect_container_workspace() {
  local container_untracked_list="${container_workspace_root}/untracked-files.list"
  local container_generated_files="${container_workspace_root}/codex-generated-files.tar.gz"

  docker exec --user 0 "${ephemeral_container}" \
    git -C "${container_checkout_dir}" status --short --untracked-files=all \
    > "${workspace_status_path}" 2>> "${log_path}" || true
  docker exec --user 0 "${ephemeral_container}" \
    git -C "${container_checkout_dir}" diff --binary HEAD \
    > "${workspace_patch_path}" 2>> "${log_path}" || true
  if docker exec --user 0 "${ephemeral_container}" bash -c \
    'set -euo pipefail; cd "$1"; git ls-files --others --exclude-standard -z > "$2"; tar --null --files-from="$2" -czf "$3"' \
    bash "${container_checkout_dir}" "${container_untracked_list}" \
    "${container_generated_files}" >> "${log_path}" 2>&1; then
    docker exec --user 0 "${ephemeral_container}" cat "${container_generated_files}" \
      > "${generated_files_path}" 2>> "${log_path}" || true
  fi
}

if ! mkdir -p "${output_dir}" || [[ ! -w "${output_dir}" ]]; then
  echo "Codex AI CI：失败（输出目录不可写：${output_dir}）" >&2
  exit 1
fi
: > "${log_path}"
: > "${report_json_path}"
: > "${report_path}"
: > "${comment_path}"
: > "${workspace_status_path}"
: > "${workspace_patch_path}"

validate_prerequisites
load_change_request_context
discover_artifact_dir

if ! workspace_dir="$(
  "${checkout_helper}" \
    "${repo_url}" \
    "${branch}" \
    "${CODEX_AI_CI_WORKSPACE_ROOT}" \
    "codex-ai" \
    "${target_sha}" \
    "${requested_base_ref}" \
    "${requested_base_sha}" 2>> "${log_path}"
)"; then
  fail_ai_ci "无法创建一次性分析 checkout"
fi
workspace_parent="$(dirname "${workspace_dir}")"
actual_sha="$(git -C "${workspace_dir}" rev-parse HEAD 2>/dev/null || true)"
if [[ "${actual_sha}" != "${target_sha}" ]]; then
  fail_ai_ci "checkout 的 SHA 与目标 SHA 不一致"
fi

if [[ "${branch}" =~ ^ci/pr-[0-9]+/.+ ]]; then
  if [[ -z "${requested_base_ref}" ]]; then
    fail_ai_ci "PR Codex 审查缺少目标分支引用"
  fi
  if [[ -z "${requested_base_sha}" ]]; then
    fail_ai_ci "PR Codex 审查缺少目标分支精确 SHA"
  fi
  if ! git -C "${workspace_dir}" cat-file -e "${requested_base_sha}^{commit}" 2>/dev/null; then
    fail_ai_ci "PR 目标分支提交在 Codex checkout 中不可用：${requested_base_sha}"
  fi
  if ! base_sha="$(
    git -C "${workspace_dir}" merge-base "${requested_base_sha}" "${target_sha}" 2>/dev/null
  )"; then
    fail_ai_ci "PR head 与目标分支提交没有共同祖先"
  fi
  base_source="merge-base"
  diff_mode="merge-base"
  diff_revisions=("${requested_base_sha}...${target_sha}")
  diff_command="git diff --find-renames ${requested_base_sha}...${target_sha}"
else
  if [[ -n "${requested_base_sha}" ]] &&
    git -C "${workspace_dir}" cat-file -e "${requested_base_sha}^{commit}" 2>/dev/null; then
    base_sha="$(
      git -C "${workspace_dir}" rev-parse "${requested_base_sha}^{commit}" 2>/dev/null
    )"
    base_source="previous-push"
  elif base_sha="$(git -C "${workspace_dir}" rev-parse "${target_sha}^" 2>/dev/null)"; then
    base_source="target-parent"
  else
    if ! base_sha="$(git -C "${workspace_dir}" mktree </dev/null)"; then
      fail_ai_ci "无法创建空树作为审查基线"
    fi
    base_source="empty-tree"
  fi
  diff_mode="two-point"
  diff_revisions=("${base_sha}" "${target_sha}")
  diff_command="git diff --find-renames ${base_sha} ${target_sha}"
fi

if ! changed_file_count="$(
  git -C "${workspace_dir}" diff --name-only --diff-filter=ACDMRTUXB \
    --find-renames "${diff_revisions[@]}" | awk 'END { print NR + 0 }'
)"; then
  fail_ai_ci "无法计算待审查的代码差异"
fi

if [[ "${analysis_mode}" == "full" ]] && diff_requires_generated_tests; then
  test_generation_expected="true"
fi

create_ephemeral_container

if [[ "${analysis_mode}" == "full" ]]; then
  if ! mode_prompt="$(
    render_prompt_template "${full_prompt_template}" \
      TEST_GENERATION_EXPECTED "${test_generation_expected}" \
      MIN_GENERATED_TEST_CASES "${CODEX_AI_CI_MIN_GENERATED_TEST_CASES}" \
      MAX_GENERATED_TEST_CASES "${CODEX_AI_CI_MAX_GENERATED_TEST_CASES}" \
      MAX_GENERATED_TEST_FILES "${CODEX_AI_CI_MAX_GENERATED_TEST_FILES}" \
      MAX_TEST_COMMANDS "${CODEX_AI_CI_MAX_TEST_COMMANDS}" \
      RECOMMENDED_COMMAND_TIMEOUT_SECONDS "${CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS}" \
      TEST_BUDGET_SECONDS "${CODEX_AI_CI_TEST_BUDGET_SECONDS}" \
      CODEX_TIMEOUT_SECONDS "${CODEX_AI_CI_TIMEOUT_SECONDS}" \
      REPORT_RESERVE_SECONDS "${CODEX_AI_CI_REPORT_RESERVE_SECONDS}"
  )"; then
    fail_ai_ci "无法渲染 Codex 完整分析提示词模板"
  fi
else
  if ! mode_prompt="$(
    render_prompt_template "${analysis_only_prompt_template}" \
      LOCAL_CI_STATUS "${local_ci_status}" \
      LOCAL_CI_LOG_PATH "${container_local_ci_log}" \
      ARTIFACT_DIR "${artifact_dir:-未识别到具体目录}"
  )"; then
    fail_ai_ci "无法渲染 Codex 只分析提示词模板"
  fi
fi

if ! prompt="$(
  render_prompt_template "${common_prompt_template}" \
    BRANCH "${branch}" \
    REQUESTED_BASE_REF "${requested_base_ref:-不适用}" \
    REQUESTED_BASE_SHA "${requested_base_sha:-不适用}" \
    BASE_SHA "${base_sha}" \
    TARGET_SHA "${target_sha}" \
    LOCAL_CI_STATUS "${local_ci_status}" \
    ANALYSIS_MODE "${analysis_mode}" \
    DIFF_MODE "${diff_mode}" \
    DIFF_COMMAND "${diff_command}" \
    CHANGE_REQUEST_CONTEXT_JSON "${change_request_context_json}" \
    MODE_INSTRUCTIONS "${mode_prompt}"
)"; then
  fail_ai_ci "无法渲染 Codex 通用提示词模板"
fi

set +e
printf '%s\n' "${prompt}" | timeout --signal=TERM --kill-after=30s \
  "${CODEX_AI_CI_TIMEOUT_SECONDS}s" \
  docker exec -i \
    --user 0 \
    --workdir "${container_checkout_dir}" \
    --env "GIT_OPTIONAL_LOCKS=0" \
    --env "HOME=/root" \
    --env "CODEX_HOME=${container_codex_home}" \
    --env "AI_ANALYSIS_MODE=${analysis_mode}" \
    --env "AI_CODEX_BIN=${container_codex_bin}" \
    --env "AI_SCHEMA_PATH=${container_schema_path}" \
    --env "AI_REPORT_PATH=${container_report_json_path}" \
    --env "AI_REASONING_EFFORT=${CODEX_AI_CI_REASONING_EFFORT}" \
    --env "AI_PYTHON_VENV_ACTIVATE=${PYTHON_VENV_ACTIVATE}" \
    --env "AI_SOURCE_ENVSETUP=${SOURCE_ENVSETUP}" \
    --env "AI_CHECKOUT_DIR=${container_checkout_dir}" \
    --env "AI_BACKEND_PATH=${BACKEND_PATH}" \
    --env "AI_BACKEND_ENVSETUP=${BACKEND_ENVSETUP}" \
    --env "AI_BACKEND_ENVSETUP_ARGS=${BACKEND_ENVSETUP_ARGS}" \
    "${ephemeral_container}" \
    bash -lc '
      bootstrap_status=0
      if [[ "${AI_ANALYSIS_MODE}" == "full" ]]; then
        set +u
        if [[ -n "${AI_PYTHON_VENV_ACTIVATE}" && -f "${AI_PYTHON_VENV_ACTIVATE}" ]]; then
          source "${AI_PYTHON_VENV_ACTIVATE}" || bootstrap_status=1
        else
          echo "Codex AI CI 环境提示：Python venv 激活脚本不存在。" >&2
          bootstrap_status=1
        fi
        if [[ "${AI_SOURCE_ENVSETUP}" == "1" && -f "${AI_CHECKOUT_DIR}/envsetup.sh" ]]; then
          source "${AI_CHECKOUT_DIR}/envsetup.sh" || bootstrap_status=1
        fi
        backend_setup="${AI_BACKEND_ENVSETUP}"
        if [[ -n "${backend_setup}" && "${backend_setup}" != /* ]]; then
          backend_setup="${AI_BACKEND_PATH}/${backend_setup}"
        fi
        if [[ -n "${backend_setup}" && -f "${backend_setup}" ]]; then
          # shellcheck disable=SC2086
          source "${backend_setup}" ${AI_BACKEND_ENVSETUP_ARGS} || bootstrap_status=1
        elif [[ -n "${backend_setup}" ]]; then
          echo "Codex AI CI 环境提示：后端环境脚本不存在。" >&2
          bootstrap_status=1
        fi
        set -u
      fi
      if [[ ${bootstrap_status} -eq 0 ]]; then
        export CODEX_AI_ENVIRONMENT_STATUS="ready"
      else
        export CODEX_AI_ENVIRONMENT_STATUS="incomplete"
      fi
      unset GITEE_TOKEN GITEE_USERNAME GIT_ASKPASS
      exec "${AI_CODEX_BIN}" exec \
        --ephemeral \
        --json \
        --sandbox danger-full-access \
        --ignore-rules \
        --config "model_reasoning_effort=\"${AI_REASONING_EFFORT}\"" \
        --output-schema "${AI_SCHEMA_PATH}" \
        --output-last-message "${AI_REPORT_PATH}" \
        -
    ' >> "${log_path}" 2>&1
exit_code=$?
set -e

docker exec --user 0 "${ephemeral_container}" cat "${container_report_json_path}" \
  > "${report_json_path}" 2>> "${log_path}" || true
collect_container_workspace

if [[ -s "${workspace_status_path}" ]]; then
  workspace_dirty="true"
fi
if grep -Fq "CODEX_AI_CI_COMPLETE" "${report_json_path}"; then
  marker_found="true"
fi
if grep -Fq '"turn.completed"' "${log_path}"; then
  turn_completed="true"
fi
if grep -Fq '"command_execution"' "${log_path}"; then
  command_executed="true"
fi
if [[ ${exit_code} -eq 0 ]]; then
  constraint_args=(
    "${report_json_path}"
    "${analysis_mode}"
    "${test_generation_expected}"
    "${CODEX_AI_CI_MAX_GENERATED_TEST_FILES}"
    "${CODEX_AI_CI_MAX_TEST_COMMANDS}"
    "${CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS}"
    "${CODEX_AI_CI_TEST_BUDGET_SECONDS}"
  )
  execution_metadata="$(
    "${PYTHON_BIN}" -c '
import json
import sys

execution = json.load(open(sys.argv[1], encoding="utf-8"))["test_execution"]
analysis_mode = sys.argv[2]
test_generation_expected = sys.argv[3] == "true"
max_files = int(sys.argv[4])
max_commands = int(sys.argv[5])
recommended_timeout = int(sys.argv[6])
test_budget = int(sys.argv[7])
generated_files = execution["generated_test_files"]
commands = execution["commands"]
durations = [float(command["duration_seconds"]) for command in commands]
max_duration = max(durations, default=0.0)
total_duration = sum(durations)
reasons = []

if analysis_mode == "full":
    if len(generated_files) > max_files:
        reasons.append(
            f"生成测试文件数量 {len(generated_files)} 超过限制 {max_files}"
        )
    if len(commands) > max_commands:
        reasons.append(
            f"测试、构建或 lint 命令数量 {len(commands)} 超过限制 {max_commands}"
        )
    if max_duration > recommended_timeout:
        reasons.append(
            f"单条命令最长耗时 {max_duration:g} 秒超过建议上限 "
            f"{recommended_timeout} 秒"
        )
    if total_duration > test_budget:
        reasons.append(
            f"测试命令累计耗时 {total_duration:g} 秒超过建议预算 "
            f"{test_budget} 秒"
        )
    if test_generation_expected and not generated_files:
        reasons.append("可测试代码改动未生成测试文件，测试证据不足")
    if test_generation_expected and not commands:
        reasons.append(
            "可测试代码改动未记录测试、构建或 lint 命令，测试证据不足"
        )
    constraint_status = "warning" if reasons else "pass"
    constraint_reason = (
        "；".join(reasons)
        if reasons
        else "生成测试文件、执行命令和报告耗时均在轻量约束范围内。"
    )
else:
    constraint_status = "not_applicable"
    constraint_reason = "只分析模式不执行测试数量和耗时约束校验。"

print(
    execution["status"],
    len(generated_files),
    len(commands),
    f"{max_duration:g}",
    f"{total_duration:g}",
    constraint_status,
    constraint_reason,
    sep="\t",
)
' "${constraint_args[@]}" 2>> "${log_path}" || true
  )"
  if [[ -n "${execution_metadata}" ]]; then
    constraint_fields=()
    IFS=$'\t' read -r -a constraint_fields <<< "${execution_metadata}"
    if [[ "${#constraint_fields[@]}" -eq 7 ]]; then
      test_execution_status="${constraint_fields[0]}"
      generated_test_file_count="${constraint_fields[1]}"
      test_command_count="${constraint_fields[2]}"
      max_test_command_duration_seconds="${constraint_fields[3]}"
      total_test_command_duration_seconds="${constraint_fields[4]}"
      constraint_status="${constraint_fields[5]}"
      constraint_reason="${constraint_fields[6]}"
    else
      execution_metadata=""
    fi
  fi

  renderer_args=(
    --input "${report_json_path}"
    --output "${report_path}"
    --comment-output "${comment_path}"
    --branch "${branch}"
    --base-sha "${base_sha}"
    --requested-base-sha "${requested_base_sha}"
    --diff-mode "${diff_mode}"
    --target-sha "${target_sha}"
    --changed-file-count "${changed_file_count}"
    --constraint-status "${constraint_status}"
    --constraint-reason "${constraint_reason}"
  )
  if report_verdict="$(
    "${PYTHON_BIN}" "${renderer_path}" "${renderer_args[@]}" 2>> "${log_path}"
  )"; then
    if [[ -n "${execution_metadata}" ]]; then
      report_format_valid="true"
    else
      report_verdict="UNKNOWN"
    fi
  else
    report_verdict="UNKNOWN"
  fi
fi

if [[ ${exit_code} -eq 124 || ${exit_code} -eq 137 ]]; then
  failure_reason="Codex 执行超过 ${CODEX_AI_CI_TIMEOUT_SECONDS} 秒硬超时"
elif [[ ${exit_code} -ne 0 ]]; then
  failure_reason="Codex exec 异常退出，退出码为 ${exit_code}"
elif [[ "${marker_found}" != "true" ]]; then
  failure_reason="结构化报告缺少 CODEX_AI_CI_COMPLETE 标记"
elif [[ "${report_format_valid}" != "true" ]]; then
  failure_reason="结构化报告未通过 schema、固定格式或中文内容校验"
elif [[ "${turn_completed}" != "true" ]]; then
  failure_reason="Codex JSONL 日志中没有 turn.completed 事件"
elif [[ "${command_executed}" != "true" ]]; then
  failure_reason="Codex 没有执行任何用于检查代码或日志的命令"
elif [[ "${analysis_mode}" == "analysis_only" && "${test_execution_status}" != "not_run" ]]; then
  failure_reason="只分析模式的 test_execution.status 必须为 not_run"
elif [[ "${analysis_mode}" == "analysis_only" && ("${generated_test_file_count}" != "0" || "${test_command_count}" != "0") ]]; then
  failure_reason="只分析模式不得声明生成测试或执行测试命令"
elif [[ "${analysis_mode}" == "analysis_only" && "${workspace_dirty}" == "true" ]]; then
  failure_reason="只分析模式意外修改了一次性 checkout"
else
  status="pass"
fi

verify_credential_integrity
if [[ "${status}" != "pass" ]]; then
  write_failure_report
fi
append_change_request_context_warning
append_credential_integrity_warning
write_summary
if [[ "${status}" == "pass" ]]; then
  echo "Codex AI CI：完成（结论 ${report_verdict}；测试状态 ${test_execution_status}；约束 ${constraint_status}；报告 ${report_path}）"
  exit 0
fi

echo "Codex AI CI：失败（${failure_reason}）"
exit 1
