#!/usr/bin/env bash
set -uo pipefail

usage="run_codex_ai_ci.sh <repo-url> <output-dir> <target-sha> <base-sha> <base-ref> <branch> [local-ci-status] [task-metadata-file] [head-sha] [head-ref]"
repo_url="${1:?usage: ${usage}}"
output_dir="${2:?usage: ${usage}}"
target_sha="${3:?usage: ${usage}}"
requested_base_sha="${4:-}"
requested_base_ref="${5:-}"
branch="${6:?usage: ${usage}}"
local_ci_status="${7:-0}"
task_metadata_file="${8:-}"
requested_head_sha="${9:-}"
requested_head_ref="${10:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_AI_CI_HOME="${CODEX_AI_CI_HOME:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CODEX_AI_CI_TIMEOUT_SECONDS="${CODEX_AI_CI_TIMEOUT_SECONDS:-3600}"
CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS="${CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS:-1500}"
CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS="${CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS:-600}"
CODEX_AI_CI_REASONING_EFFORT="${CODEX_AI_CI_REASONING_EFFORT:-medium}"
CODEX_AI_CI_WORKSPACE_ROOT="${CODEX_AI_CI_WORKSPACE_ROOT:-${TMPDIR:-/tmp}/triton-anchor-codex-ai}"
CODEX_AI_CI_MIN_GENERATED_TEST_CASES="${CODEX_AI_CI_MIN_GENERATED_TEST_CASES:-1}"
CODEX_AI_CI_MAX_GENERATED_TEST_CASES="${CODEX_AI_CI_MAX_GENERATED_TEST_CASES:-15}"
CODEX_AI_CI_MAX_GENERATED_TEST_FILES="${CODEX_AI_CI_MAX_GENERATED_TEST_FILES:-5}"
CODEX_AI_CI_MAX_TEST_COMMANDS="${CODEX_AI_CI_MAX_TEST_COMMANDS:-30}"
CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS="${CODEX_AI_CI_RECOMMENDED_COMMAND_TIMEOUT_SECONDS:-600}"
CODEX_AI_CI_TEST_BUDGET_SECONDS="${CODEX_AI_CI_TEST_BUDGET_SECONDS:-2700}"
CODEX_AI_CI_REPORT_RESERVE_SECONDS="${CODEX_AI_CI_REPORT_RESERVE_SECONDS:-450}"
LOCAL_CI_CONTAINER="${LOCAL_CI_CONTAINER:-anchor-sophgo-ci}"
LOCAL_CI_ARTIFACT_ROOT="${LOCAL_CI_ARTIFACT_ROOT:-/workspace/local-ci-artifacts}"
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
container_changed_files_manifest="${container_input_dir}/codex-changed-files-manifest.json"

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
task_metadata_validator="${LOCAL_CI_ROOT}/shared/validate_task_metadata.py"
task_metadata_output_path="${output_dir}/task-metadata.json"
prompt_dir="${SCRIPT_DIR}/prompts"
success_prompt_template="${prompt_dir}/codex_ai_success.md"
failure_prompt_template="${prompt_dir}/codex_ai_failure.md"
changed_files_manifest_path="${output_dir}/codex-changed-files-manifest.json"

status="fail"
exit_code=1
actual_sha=""
base_sha=""
base_source=""
diff_mode="unresolved"
diff_command=""
diff_revisions=()
changed_file_count=0
changed_files_manifest_json="[]"
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
startup_progress="false"
startup_timed_out="false"
prepare_timed_out="false"
prepare_timeout_phase=""
prepare_started_seconds=0
prepare_deadline_seconds=0
prepare_duration_seconds="UNKNOWN"
snapshot_duration_seconds="UNKNOWN"
container_start_duration_seconds="UNKNOWN"
input_setup_duration_seconds="UNKNOWN"
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
review_context_profile="unclassified"
review_context_hint="尚未根据变更文件生成审查上下文策略。"
changed_file_groups_json="{}"
failure_code=""
start_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_seconds="${SECONDS}"
if [[ "${local_ci_status}" != "0" ]]; then
  analysis_mode="analysis_only"
  constraint_reason="Local CI 失败诊断尚未完成轻量约束校验。"
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
    echo "tested_sha: ${target_sha}"
    echo "actual_sha: ${actual_sha}"
    echo "requested_base_sha: ${requested_base_sha}"
    echo "requested_base_ref: ${requested_base_ref}"
    echo "requested_head_sha: ${requested_head_sha}"
    echo "requested_head_ref: ${requested_head_ref}"
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
    echo "workspace_dir: ${workspace_dir}"
    echo "container_workspace_dir: ${container_checkout_dir}"
    echo "artifact_dir: ${artifact_dir}"
    echo "output_dir: ${output_dir}"
    echo "changed_file_count: ${changed_file_count}"
    echo "review_context_profile: ${review_context_profile}"
    echo "review_context_hint: ${review_context_hint}"
    echo "changed_file_groups_json: ${changed_file_groups_json}"
    echo "started_at: ${start_time}"
    echo "duration_seconds: ${duration_seconds}"
    echo "timeout_seconds: ${CODEX_AI_CI_TIMEOUT_SECONDS}"
    echo "prepare_timeout_seconds: ${CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS}"
    echo "prepare_timed_out: ${prepare_timed_out}"
    echo "prepare_timeout_phase: ${prepare_timeout_phase}"
    echo "prepare_duration_seconds: ${prepare_duration_seconds}"
    echo "snapshot_duration_seconds: ${snapshot_duration_seconds}"
    echo "container_start_duration_seconds: ${container_start_duration_seconds}"
    echo "input_setup_duration_seconds: ${input_setup_duration_seconds}"
    echo "reasoning_effort: ${CODEX_AI_CI_REASONING_EFFORT}"
    echo "startup_timeout_seconds: ${CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS}"
    echo "startup_progress: ${startup_progress}"
    echo "startup_timed_out: ${startup_timed_out}"
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
    echo "failure_code: ${failure_code}"
    echo "failure_reason: ${failure_reason}"
  } > "${summary_path}"
}

write_failure_report() {
  local rendered_diff_mode="two-point"
  local repository_root_args=()
  local fallback_assessment_label="无法判断"
  local fallback_contributor_goal="Codex AI 自动审查未完成，未能可靠归纳贡献者的修改目标。"
  local fallback_expected_behavior="Codex AI 自动审查未完成，未能可靠归纳贡献者声明的预期行为。"
  local fallback_implementation_summary="当前没有足够证据判断代码是否实现了声明目标。"
  if [[ "${diff_mode}" == "merge-base" ]]; then
    rendered_diff_mode="merge-base"
  fi
  if [[ "${change_request_context_status}" == "not_applicable" ]]; then
    fallback_assessment_label="不适用"
    fallback_contributor_goal="当前任务不是 PR，因此没有需要对照的贡献者功能声明。"
    fallback_expected_behavior="当前任务不是 PR，因此贡献者预期行为不适用。"
    fallback_implementation_summary="当前任务不是 PR，不进行贡献者声明对照；Codex AI 审查未完成。"
  fi
  if [[ -n "${workspace_dir}" && -d "${workspace_dir}" ]]; then
    repository_root_args=(--repository-root "${workspace_dir}")
  fi
  report_verdict="WARNING"
  test_execution_status="insufficient_evidence"
  generated_test_file_count="0"
  test_command_count="0"
  max_test_command_duration_seconds="0"
  total_test_command_duration_seconds="0"
  constraint_status="warning"
  constraint_reason="Codex AI 自动审查未完成，无法确认测试执行是否符合轻量约束。"

  if ! "${PYTHON_BIN}" - "${failure_reason:-未知原因}" \
    "${changed_files_manifest_path}" "${change_request_context_status}" \
    > "${report_json_path}" <<'PY'
import json
import sys
from pathlib import Path

failure_reason = sys.argv[1]
manifest_path = Path(sys.argv[2])
context_status = sys.argv[3]
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    manifest = []

changed_files = []
for item in manifest if isinstance(manifest, list) else []:
    if not isinstance(item, dict):
        continue
    path = item.get("path")
    change_type = item.get("change_type")
    if not isinstance(path, str) or not isinstance(change_type, str):
        continue
    changed_files.append({
        "path": path,
        "change_type": change_type,
        "summary": "AI 审查未完成，未能可靠归纳该文件的改动。",
        "impact": "该文件的行为影响仍需人工检查。",
        "validation_strategy": "未执行：Codex AI 自动审查未完成，没有取得该文件的可信验证结果。",
    })

behavior_coverage = {}
labels = {
    "normal": "正常路径",
    "boundary": "边界路径",
    "error": "错误路径",
    "compatibility": "兼容路径",
    "integration": "集成路径",
}
for key, label in labels.items():
    behavior_coverage[key] = {
        "scope": f"本次未能可靠检查{label}。",
        "strategy": "修复 AI 执行问题后重新分析代码差异并进行定向验证。",
        "result": "分析未完成，当前没有可信结论。",
    }

document = {
    "verdict": "WARNING",
    "summary": f"Codex AI 自动审查未完成：{failure_reason}。本次没有生成可信的结构化审查摘要。",
    "merge_recommendation": "请先排查 AI 执行问题并重新运行，当前不要仅依据本次 AI 结果决定合入。",
    "change_request_assessment": {
        "status": "not_applicable" if context_status == "not_applicable" else "not_assessable",
        "contributor_goal": (
            "当前任务不是 PR，因此没有需要对照的贡献者功能声明。"
            if context_status == "not_applicable"
            else "Codex AI 自动审查未完成，未能可靠归纳贡献者的修改目标。"
        ),
        "expected_behavior": (
            "当前任务不是 PR，因此贡献者预期行为不适用。"
            if context_status == "not_applicable"
            else "Codex AI 自动审查未完成，未能可靠归纳贡献者声明的预期行为。"
        ),
        "implementation_summary": (
            "当前任务不是 PR，不进行贡献者声明对照；Codex AI 审查未完成。"
            if context_status == "not_applicable"
            else "Codex AI 自动审查未完成，不能判断当前代码是否实现了声明目标。"
        ),
        "evidence": [
            f"Codex AI 自动审查执行失败：{failure_reason}。当前没有完整的代码审查证据。"
        ],
    },
    "changed_files": changed_files,
    "behavior_coverage": behavior_coverage,
    "findings": [],
    "suggested_tests": [],
    "residual_risks": [
        "当前代码差异仍需人工检查，或在修复执行环境后重新运行 Codex AI 自动审查。"
    ],
    "test_execution": {
        "status": "insufficient_evidence",
        "summary": [
            "Codex AI 自动审查未完成。",
            "本次没有获得可信的测试执行结论。",
        ],
        "generated_test_files": [],
        "commands": [],
    },
    "completion_marker": "CODEX_AI_CI_COMPLETE",
}
json.dump(document, sys.stdout, ensure_ascii=False, indent=2)
sys.stdout.write("\n")
PY
  then
    :
  fi

  if [[ -r "${renderer_path}" && -r "${changed_files_manifest_path}" ]] && \
    "${PYTHON_BIN}" "${renderer_path}" \
      --input "${report_json_path}" \
      --output "${report_path}" \
      --comment-output "${comment_path}" \
      --branch "${branch}" \
      --base-sha "${base_sha:-不可用}" \
      --requested-base-sha "${requested_base_sha}" \
      --diff-mode "${rendered_diff_mode}" \
      --target-sha "${target_sha}" \
      --head-sha "${requested_head_sha}" \
      --tested-sha-kind "$([[ "${branch}" =~ ^ci/pr-[0-9]+/.+ ]] && printf '%s' pr_merge || printf '%s' commit)" \
      --local-ci-status "${local_ci_status}" \
      --changed-file-count "${changed_file_count}" \
      --changed-files-manifest "${changed_files_manifest_path}" \
      --constraint-status "${constraint_status}" \
      --constraint-reason "${constraint_reason}" \
      "${repository_root_args[@]}" \
      >/dev/null 2>> "${log_path}"; then
    return 0
  fi

  {
    echo "# Codex AI 自动审查报告"
    echo
    echo "## 元数据"
    echo
    echo "| 字段 | 值 |"
    echo "| --- | --- |"
    echo "| 报告格式 | \`triton-anchor-codex-ai-report/v3\` |"
    echo "| 分支 | \`${branch}\` |"
    echo "| 请求的基础提交 | \`${requested_base_sha:-不可用}\` |"
    echo "| PR Head 提交 | \`${requested_head_sha:-不可用}\` |"
    echo "| 实际审查起点 | \`${base_sha:-不可用}\` |"
    echo "| 差异模式 | \`${diff_mode}\` |"
    echo "| 测试提交 | \`${target_sha}\` |"
    echo "| 变更文件数 | ${changed_file_count} |"
    echo "| 生成时间（UTC） | \`${start_time}\` |"
    echo
    echo "## 结论"
    echo
    echo "**警告**"
    echo
    echo "## 摘要"
    echo
    echo "Codex AI 自动审查未完成：${failure_reason}"
    echo
    echo "## 贡献者目标与实现情况"
    echo
    echo "- 判断：${fallback_assessment_label}"
    echo "- 修改目标：${fallback_contributor_goal}"
    echo "- 预期行为：${fallback_expected_behavior}"
    echo "- 实现情况：${fallback_implementation_summary}"
    echo "- 判断依据："
    echo "  - Codex AI 自动审查未完成，当前没有足够证据判断贡献者声明和实际实现是否一致。"
    echo
    echo "## 合入建议"
    echo
    echo "请先排查 AI 执行问题并重新运行，当前不要仅依据本次 AI 结果决定合入。"
    echo
    echo "## 具体文件变更"
    echo
    echo "分析未完成，无法可靠生成文件级变更表。"
    echo
    echo "## 行为覆盖"
    echo
    echo "正常、边界、错误、兼容和集成路径均未获得可信结论。"
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
    echo "- 状态：证据不足"
    echo "- 摘要：Codex AI 自动审查未完成，未获得可信的测试结果。"
    echo
    echo "## 剩余风险"
    echo
    echo "- 本次 Codex AI 自动审查未完成，当前代码差异仍需人工检查。"
    echo
    echo "## 执行标记"
    echo
    echo "CODEX_AI_CI_FAILED"
  } > "${report_path}"
  {
    echo "## Codex AI 自动审查"
    echo
    echo "> Codex AI 自动审查仅供参考且不阻塞合入；本地确定性 CI 检查结果才是合入门禁。"
    echo
    echo "### 审查摘要"
    echo
    echo "- Codex AI 审查结论：**警告**"
    if [[ "${local_ci_status}" == "0" ]]; then
      echo "- 本地确定性 CI 检查：已通过；本次失败只影响 Codex AI 自动审查，不改变门禁结果。"
    else
      echo "- 本地确定性 CI 检查：未通过；需要先根据检查日志和复测结果判断合入风险。"
    fi
    echo "- 合入建议：请先排查 Codex AI 自动审查执行问题并重新运行，再决定是否合入。"
    echo
    echo "$(codex_failure_public_reason "${failure_code:-codex_execution_failed}")。"
    echo
    echo "### 贡献者目标与实现情况"
    echo
    echo "- 判断：**${fallback_assessment_label}**"
    echo "- 贡献者目标：${fallback_contributor_goal}"
    echo "- 预期效果：${fallback_expected_behavior}"
    echo "- 当前实现情况：${fallback_implementation_summary}"
    echo "- 判断依据："
    echo "  - Codex AI 自动审查未完成，当前没有足够证据可靠判断贡献者声明和实际实现是否一致。"
    echo
    echo "### 需要处理的问题"
    echo
    echo "未生成产品代码问题；Codex AI 自动审查执行未完成。"
    echo
    echo "### 验证情况"
    echo
    echo "- 补充验证结果：**未获得可信结果**"
    echo "- 说明："
    echo "  - Codex AI 自动审查未完成。"
    echo "  - 本次没有获得可信的补充验证结果。"
    echo
    echo "### 变更文件"
    echo
    echo "<details>"
    echo "<summary>展开文件级变更表</summary>"
    echo
    echo "分析未完成，无法可靠生成文件级变更表。"
    echo
    echo "</details>"
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

codex_failure_code_for_reason() {
  local reason="$1"
  case "${reason}" in
    *"Codex CLI"* | *"找不到可执行的 Codex"*) echo "codex_cli_unavailable" ;;
    *"CODEX_AI_CI_HOME"* | *"独立凭据"* | *"config.toml"* | *"auth.json"*) echo "credential_validation_failed" ;;
    *"提示词"* | *"渲染"*) echo "prompt_render_failed" ;;
    *"容器准备阶段"* | *"环境快照超时"*) echo "container_prepare_timeout" ;;
    *"硬超时"*) echo "timeout" ;;
    *"启动阶段"* | *"首个有效进展"*) echo "startup_timeout" ;;
    *"CODEX_AI_CI_COMPLETE"*) echo "missing_completion_marker" ;;
    *"turn.completed"*) echo "missing_turn_completed" ;;
    *"没有执行任何"*) echo "no_command_executed" ;;
    *"schema"* | *"固定格式"* | *"中文内容"*) echo "schema_validation_failed" ;;
    *"finding"* | *"行范围"* | *"行号"*) echo "invalid_finding_location" ;;
    *"容器"* | *"Docker socket"* | *"镜像"*) echo "container_setup_failed" ;;
    *"checkout"* | *"差异"* | *"变更文件清单"*) echo "checkout_or_diff_failed" ;;
    *"宿主机缺少"* | *"Python"*) echo "prerequisite_failed" ;;
    *) echo "codex_execution_failed" ;;
  esac
}

codex_failure_public_reason() {
  case "$1" in
    codex_cli_unavailable) echo "Codex AI 自动审查工具在当前环境中不可用" ;;
    credential_validation_failed) echo "Codex 审查凭据校验未通过" ;;
    prompt_render_failed) echo "Codex 审查输入准备失败" ;;
    container_prepare_timeout) echo "Codex 审查运行环境准备超时" ;;
    timeout) echo "Codex 自动审查执行超时" ;;
    startup_timeout) echo "Codex 自动审查启动阶段超时" ;;
    missing_completion_marker | missing_turn_completed) echo "Codex 自动审查没有完整结束" ;;
    no_command_executed) echo "Codex 自动审查没有获得可核验的补充验证结果" ;;
    schema_validation_failed) echo "Codex 审查结果格式校验未通过" ;;
    invalid_finding_location) echo "Codex 问题定位信息校验未通过" ;;
    container_setup_failed) echo "Codex 审查运行环境启动失败" ;;
    checkout_or_diff_failed) echo "Codex 审查代码或差异准备失败" ;;
    prerequisite_failed) echo "Codex 审查运行环境缺少必要组件" ;;
    *) echo "Codex 自动审查执行异常" ;;
  esac
}

set_failure_reason() {
  failure_reason="$1"
  if [[ -z "${failure_code}" ]]; then
    failure_code="$(codex_failure_code_for_reason "${failure_reason}")"
  fi
}

fail_ai_ci() {
  set_failure_reason "$1"
  verify_credential_integrity
  write_failure_report
  append_change_request_context_warning
  append_credential_integrity_warning
  echo "Codex AI CI 失败：${failure_code} ${failure_reason}" >> "${log_path}"
  write_summary
  echo "Codex AI CI：失败（${failure_code}：${failure_reason}）"
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
    change_request_context_reason="未取得与当前 PR 测试提交匹配的功能声明元数据；继续依据代码差异和测试证据分析。"
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
      --target-sha "${target_sha}" \
      --base-sha "${requested_base_sha}" \
      --head-sha "${requested_head_sha}" 2>&1
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
for key in (
    "event_kind",
    "base_branch",
    "base_sha",
    "head_branch",
    "head_sha",
    "head_repo",
    "tested_ref",
    "tested_sha",
    "tested_sha_kind",
):
    if key in metadata:
        context[key] = metadata[key]
print(metadata["pr_number"])
print(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
print(metadata.get("base_sha", ""))
print(metadata.get("head_sha", ""))
print(metadata.get("base_task_ref", ""))
print(metadata.get("head_task_ref", ""))
PY
  )
  if [[ "${#context_parts[@]}" -lt 2 || -z "${context_parts[1]}" ]]; then
    rm -f -- "${task_metadata_output_path}"
    change_request_context_status="invalid"
    change_request_context_reason="规范化后的 PR 功能声明元数据无法读取；继续依据代码差异和测试证据分析。"
    change_request_context_json="$(build_unavailable_change_request_context)" || \
      fail_ai_ci "无法生成 PR 元数据读取失败上下文"
    echo "${change_request_context_reason}" >> "${log_path}"
    return 0
  fi

  local metadata_base_sha="${context_parts[2]:-}"
  local metadata_head_sha="${context_parts[3]:-}"
  local metadata_base_ref="${context_parts[4]:-}"
  local metadata_head_ref="${context_parts[5]:-}"
  if [[ -z "${requested_base_sha}" && -n "${metadata_base_sha}" ]]; then
    requested_base_sha="${metadata_base_sha}"
  fi
  if [[ -z "${requested_head_sha}" && -n "${metadata_head_sha}" ]]; then
    requested_head_sha="${metadata_head_sha}"
  fi
  if [[ -z "${requested_base_ref}" && -n "${metadata_base_ref}" ]]; then
    requested_base_ref="${metadata_base_ref}"
  fi
  if [[ -z "${requested_head_ref}" && -n "${metadata_head_ref}" ]]; then
    requested_head_ref="${metadata_head_ref}"
  fi

  change_request_context_status="available"
  change_request_context_reason="已校验并载入与当前 PR 测试提交匹配的功能声明元数据。"
  change_request_context_pr_number="${context_parts[0]}"
  change_request_context_json="${context_parts[1]}"
}

validate_prerequisites() {
  local integer_name
  local prompt_template
  local integer_names=(
    CODEX_AI_CI_TIMEOUT_SECONDS
    CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS
    CODEX_AI_CI_MIN_GENERATED_TEST_CASES
    CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS
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
  if [[ ! -r "${checkout_helper}" ]]; then
    fail_ai_ci "checkout helper 不可读：${checkout_helper}"
  fi
  if [[ ! -r "${credentials_validator}" ]]; then
    fail_ai_ci "独立凭据校验器不可读：${credentials_validator}"
  fi
  if [[ ! -r "${task_metadata_validator}" ]]; then
    fail_ai_ci "PR 功能声明元数据校验器不可读：${task_metadata_validator}"
  fi
  for prompt_template in \
    "${success_prompt_template}" \
    "${failure_prompt_template}"; do
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
  local candidate=""
  local resolved_root=""
  local resolved_candidate=""
  if [[ ! -f "${output_dir}/local-ci.log" ]]; then
    return 0
  fi
  candidate="$(
    sed -n 's/.*Artifacts are in \([^[:space:]]*\).*/\1/p' \
      "${output_dir}/local-ci.log" | tail -n 1
  )"
  if [[ -z "${candidate}" ]]; then
    return 0
  fi
  if [[ -z "${ephemeral_container}" ]]; then
    artifact_dir=""
    return 0
  fi
  resolved_root="$(
    docker exec --user 0 "${ephemeral_container}" \
      readlink -e -- "${LOCAL_CI_ARTIFACT_ROOT}" 2>> "${log_path}" || true
  )"
  resolved_candidate="$(
    docker exec --user 0 "${ephemeral_container}" \
      readlink -e -- "${candidate}" 2>> "${log_path}" || true
  )"
  if [[ -z "${resolved_root}" || -z "${resolved_candidate}" \
    || "${resolved_candidate}" == "${resolved_root}" \
    || "${resolved_candidate}" != "${resolved_root}"/* ]]; then
    echo "忽略不在预期 artifact 根目录中的日志路径：${candidate}" >> "${log_path}"
    artifact_dir=""
    return 0
  fi
  artifact_dir="${resolved_candidate}"
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

generate_changed_files_manifest() {
  local diff_args=(
    -C "${workspace_dir}"
    diff
    --name-status
    -z
    --diff-filter=ACDMRTUXB
    --find-renames
    "${diff_revisions[@]}"
  )
  if ! git "${diff_args[@]}" | "${PYTHON_BIN}" -c '
import json
import sys

fields = sys.stdin.buffer.read().split(b"\0")
if fields and fields[-1] == b"":
    fields.pop()

manifest = []
index = 0
while index < len(fields):
    status = fields[index].decode("ascii")
    index += 1
    code = status[:1]
    if code in {"R", "C"}:
        if index + 1 >= len(fields):
            raise SystemExit("incomplete rename/copy record in git diff")
        previous_path = fields[index].decode("utf-8", "replace")
        path = fields[index + 1].decode("utf-8", "replace")
        index += 2
        if code == "R":
            manifest.append({
                "path": path,
                "change_type": "renamed",
                "previous_path": previous_path,
            })
        else:
            manifest.append({"path": path, "change_type": "added"})
        continue
    if index >= len(fields):
        raise SystemExit("incomplete path record in git diff")
    path = fields[index].decode("utf-8", "replace")
    index += 1
    change_type = {
        "A": "added",
        "D": "deleted",
        "M": "modified",
        "T": "modified",
        "U": "modified",
        "X": "modified",
        "B": "modified",
    }.get(code)
    if change_type is None:
        raise SystemExit(f"unsupported git diff status: {status}")
    manifest.append({"path": path, "change_type": change_type})

paths = [item["path"] for item in manifest]
if len(paths) != len(set(paths)):
    raise SystemExit("duplicate path in git diff manifest")
json.dump(manifest, sys.stdout, ensure_ascii=False, separators=(",", ":"))
sys.stdout.write("\n")
' > "${changed_files_manifest_path}"; then
    return 1
  fi

  if ! changed_file_count="$(${PYTHON_BIN} -c '
import json
import sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))))
' "${changed_files_manifest_path}")"; then
    return 1
  fi
  changed_files_manifest_json="$(<"${changed_files_manifest_path}")"
}

classify_review_context() {
  local context_lines=()
  mapfile -t context_lines < <(
    "${PYTHON_BIN}" "${SCRIPT_DIR}/classify_codex_review_context.py" \
      "${changed_files_manifest_path}" "${analysis_mode}"
  )
  if [[ "${#context_lines[@]}" -ne 3 ]]; then
    review_context_profile="unclassified"
    review_context_hint="无法生成文件分组摘要；按标准项目专项审查执行。"
    changed_file_groups_json="{}"
    return 0
  fi
  review_context_profile="${context_lines[0]}"
  review_context_hint="${context_lines[1]}"
  changed_file_groups_json="${context_lines[2]}"
  printf '%s\n' "${changed_file_groups_json}" > "${output_dir}/codex-context-summary.json"
}

run_prepare_command() {
  local phase="$1"
  shift
  local remaining_seconds="$((prepare_deadline_seconds - SECONDS))"
  local command_started_seconds="${SECONDS}"
  local command_exit=0

  if ((remaining_seconds <= 0)); then
    prepare_timed_out="true"
    prepare_timeout_phase="${phase}"
    return 124
  fi

  echo "Codex 容器准备开始：${phase}（剩余预算 ${remaining_seconds} 秒）。" >> "${log_path}"
  timeout --signal=TERM --kill-after=30s "${remaining_seconds}s" \
    "$@" >> "${log_path}" 2>&1
  command_exit=$?
  echo "Codex 容器准备结束：${phase}（耗时 $((SECONDS - command_started_seconds)) 秒，退出码 ${command_exit}）。" \
    >> "${log_path}"
  if [[ ${command_exit} -eq 124 || ${command_exit} -eq 137 ]]; then
    prepare_timed_out="true"
    prepare_timeout_phase="${phase}"
  fi
  return "${command_exit}"
}

run_prepare_capture() {
  local output_variable="$1"
  local phase="$2"
  shift 2
  local remaining_seconds="$((prepare_deadline_seconds - SECONDS))"
  local command_started_seconds="${SECONDS}"
  local command_exit=0
  local captured_output=""

  if ((remaining_seconds <= 0)); then
    prepare_timed_out="true"
    prepare_timeout_phase="${phase}"
    return 124
  fi

  echo "Codex 容器准备开始：${phase}（剩余预算 ${remaining_seconds} 秒）。" >> "${log_path}"
  captured_output="$(
    timeout --signal=TERM --kill-after=30s "${remaining_seconds}s" \
      "$@" 2>> "${log_path}"
  )"
  command_exit=$?
  echo "Codex 容器准备结束：${phase}（耗时 $((SECONDS - command_started_seconds)) 秒，退出码 ${command_exit}）。" \
    >> "${log_path}"
  if [[ ${command_exit} -eq 124 || ${command_exit} -eq 137 ]]; then
    prepare_timed_out="true"
    prepare_timeout_phase="${phase}"
  fi
  if [[ ${command_exit} -eq 0 ]]; then
    printf -v "${output_variable}" '%s' "${captured_output}"
  fi
  return "${command_exit}"
}

fail_prepare_step() {
  local reason="$1"
  prepare_duration_seconds="$((SECONDS - prepare_started_seconds))"
  if [[ "${snapshot_duration_seconds}" == "UNKNOWN" ]]; then
    snapshot_duration_seconds="$((SECONDS - phase_started_seconds))"
  elif [[ "${container_start_duration_seconds}" == "UNKNOWN" ]]; then
    container_start_duration_seconds="$((SECONDS - phase_started_seconds))"
  elif [[ "${input_setup_duration_seconds}" == "UNKNOWN" ]]; then
    input_setup_duration_seconds="$((SECONDS - phase_started_seconds))"
  fi
  if [[ "${prepare_timed_out}" == "true" ]]; then
    failure_code="container_prepare_timeout"
    fail_ai_ci "Codex 容器准备阶段超过 ${CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS} 秒（阶段：${prepare_timeout_phase}）"
  fi
  fail_ai_ci "${reason}"
}

create_ephemeral_container() {
  local resource_key
  local workspace_rw
  local container_running
  local mount_destinations
  local copied_sha
  local phase_started_seconds
  resource_key="$(date -u +%Y%m%dT%H%M%SZ)-${target_sha:0:12}-$$"
  resource_key="${resource_key,,}"
  ephemeral_container="anchor-codex-ai-${resource_key}"
  ephemeral_image="triton-anchor-codex-ai-snapshot:${resource_key}"
  prepare_started_seconds="${SECONDS}"
  prepare_deadline_seconds="$((SECONDS + 10#${CODEX_AI_CI_PREPARE_TIMEOUT_SECONDS}))"

  echo "正在从 ${LOCAL_CI_CONTAINER} 创建本次任务的临时镜像 ${ephemeral_image}。" >> "${log_path}"
  phase_started_seconds="${SECONDS}"
  if ! run_prepare_command "environment_snapshot" \
    docker commit "${LOCAL_CI_CONTAINER}" "${ephemeral_image}"; then
    snapshot_duration_seconds="$((SECONDS - phase_started_seconds))"
    fail_prepare_step "无法从本次 Local CI 容器创建环境快照"
  fi
  snapshot_duration_seconds="$((SECONDS - phase_started_seconds))"

  phase_started_seconds="${SECONDS}"
  if ! run_prepare_command "container_start" docker run -dit \
    --name "${ephemeral_container}" \
    --hostname "${ephemeral_container}" \
    --label "triton-anchor.role=codex-ai" \
    --label "triton-anchor.target-sha=${target_sha}" \
    --volumes-from "${LOCAL_CI_CONTAINER}:ro" \
    --entrypoint /bin/bash \
    "${ephemeral_image}" \
    -lc 'trap : TERM INT; while :; do sleep 3600; done'; then
    container_start_duration_seconds="$((SECONDS - phase_started_seconds))"
    fail_prepare_step "无法启动本次任务的临时 Codex 容器"
  fi

  if ! run_prepare_capture container_running "inspect_container_running" \
    docker inspect --format '{{.State.Running}}' "${ephemeral_container}"; then
    fail_prepare_step "无法检查临时 Codex 容器状态"
  fi
  if [[ "${container_running}" != "true" ]]; then
    fail_prepare_step "临时 Codex 容器启动后未保持运行"
  fi
  if ! run_prepare_capture mount_destinations "inspect_container_mounts" \
    docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
      "${ephemeral_container}"; then
    fail_prepare_step "无法检查临时 Codex 容器挂载"
  fi
  if grep -Fxq '/var/run/docker.sock' <<< "${mount_destinations}"; then
    fail_prepare_step "临时 Codex 容器意外挂载了 Docker socket"
  fi
  if ! run_prepare_capture workspace_rw "inspect_workspace_mode" \
    docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{println .RW}}{{end}}{{end}}' \
      "${ephemeral_container}"; then
    fail_prepare_step "无法检查临时 Codex 容器的 /workspace 挂载模式"
  fi
  if [[ "${workspace_rw}" != "false" ]]; then
    fail_prepare_step "临时 Codex 容器没有以只读方式复用 /workspace"
  fi
  container_start_duration_seconds="$((SECONDS - phase_started_seconds))"

  phase_started_seconds="${SECONDS}"
  if ! run_prepare_command "workspace_setup" docker exec --user 0 "${ephemeral_container}" \
    mkdir -p \
      "${container_codex_home}" \
      "$(dirname "${container_codex_bin}")" \
      "${container_checkout_dir}" \
      "${container_input_dir}"; then
    fail_prepare_step "无法在临时容器中创建 Codex 工作目录"
  fi

  if ! run_prepare_command "copy_codex_cli" docker cp "${host_codex_bin}" \
    "${ephemeral_container}:${container_codex_bin}"; then
    fail_prepare_step "无法把 Codex CLI 复制到临时容器"
  fi
  if ! run_prepare_command "configure_codex_cli" \
    docker exec --user 0 "${ephemeral_container}" \
      chmod +x "${container_codex_bin}"; then
    fail_prepare_step "无法设置容器内 Codex CLI 的执行权限"
  fi
  for config_file in config.toml auth.json; do
    if ! run_prepare_command "copy_${config_file}" docker cp \
      "${CODEX_AI_CI_HOME}/${config_file}" \
      "${ephemeral_container}:${container_codex_home}/${config_file}"; then
      fail_prepare_step "无法把 ${config_file} 复制到临时容器"
    fi
  done

  if ! run_prepare_command "copy_checkout" docker cp "${workspace_dir}/." \
    "${ephemeral_container}:${container_checkout_dir}"; then
    fail_prepare_step "无法把经过验证的 checkout 复制到临时容器"
  fi
  if ! run_prepare_command "copy_report_schema" docker cp "${schema_path}" \
    "${ephemeral_container}:${container_schema_path}"; then
    fail_prepare_step "无法把报告 schema 复制到临时容器"
  fi
  if ! run_prepare_command "copy_changed_files_manifest" docker cp \
    "${changed_files_manifest_path}" \
    "${ephemeral_container}:${container_changed_files_manifest}"; then
    fail_prepare_step "无法把变更文件清单复制到临时容器"
  fi
  if [[ -f "${output_dir}/local-ci.log" ]]; then
    if ! run_prepare_command "copy_local_ci_log" docker cp \
      "${output_dir}/local-ci.log" \
      "${ephemeral_container}:${container_local_ci_log}"; then
      fail_prepare_step "无法把 Local CI 日志复制到临时容器"
    fi
  fi

  if ! run_prepare_command "configure_workspace_ownership" \
    docker exec --user 0 "${ephemeral_container}" \
      chown -R 0:0 \
        "${container_codex_home}" \
        "${container_workspace_root}" \
        "${container_codex_bin}"; then
    fail_prepare_step "无法修正临时容器内 Codex 文件的所有权"
  fi
  if ! run_prepare_command "configure_credential_permissions" \
    docker exec --user 0 "${ephemeral_container}" \
      chmod 600 \
        "${container_codex_home}/config.toml" \
        "${container_codex_home}/auth.json"; then
    fail_prepare_step "无法收紧临时容器内 Codex 配置文件权限"
  fi

  copied_sha="$(
    docker exec --user 0 "${ephemeral_container}" \
      git -C "${container_checkout_dir}" rev-parse HEAD 2>> "${log_path}" || true
  )"
  copied_sha="${copied_sha//$'\r'/}"
  if [[ "${copied_sha}" != "${target_sha}" ]]; then
    fail_prepare_step "容器内 checkout 的 SHA 与目标 SHA 不一致"
  fi
  if ! run_prepare_command "verify_codex_cli" \
    docker exec --user 0 "${ephemeral_container}" \
      "${container_codex_bin}" --version; then
    fail_prepare_step "Codex CLI 无法在临时容器中启动"
  fi
  input_setup_duration_seconds="$((SECONDS - phase_started_seconds))"
  prepare_duration_seconds="$((SECONDS - prepare_started_seconds))"
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
printf '[]\n' > "${changed_files_manifest_path}"

validate_prerequisites
load_change_request_context

if ! workspace_dir="$(
  bash "${checkout_helper}" \
    "${repo_url}" \
    "${branch}" \
    "${CODEX_AI_CI_WORKSPACE_ROOT}" \
    "codex-ai" \
    "${target_sha}" \
    "${requested_base_ref}" \
    "${requested_base_sha}" \
    "${requested_head_ref}" \
    "${requested_head_sha}" 2>> "${log_path}"
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
  if [[ -z "${requested_head_sha}" ]]; then
    fail_ai_ci "PR Codex 审查缺少贡献分支精确 SHA"
  fi
  if ! git -C "${workspace_dir}" cat-file -e "${requested_base_sha}^{commit}" 2>/dev/null; then
    fail_ai_ci "PR 目标分支提交在 Codex checkout 中不可用：${requested_base_sha}"
  fi
  if ! git -C "${workspace_dir}" cat-file -e "${requested_head_sha}^{commit}" 2>/dev/null; then
    fail_ai_ci "PR head 提交在 Codex checkout 中不可用：${requested_head_sha}"
  fi
  if ! base_sha="$(
    git -C "${workspace_dir}" merge-base "${requested_base_sha}" "${requested_head_sha}" 2>/dev/null
  )"; then
    fail_ai_ci "PR head 与目标分支提交没有共同祖先"
  fi
  base_source="merge-base"
  diff_mode="merge-base"
  diff_revisions=("${requested_base_sha}...${requested_head_sha}")
  diff_command="git diff --find-renames ${requested_base_sha}...${requested_head_sha}"
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

if ! generate_changed_files_manifest; then
  fail_ai_ci "无法生成待审查代码差异的标准文件清单"
fi
classify_review_context

if diff_requires_generated_tests; then
  test_generation_expected="true"
fi

create_ephemeral_container
discover_artifact_dir

if [[ "${analysis_mode}" == "full" ]]; then
  selected_prompt_template="${success_prompt_template}"
else
  selected_prompt_template="${failure_prompt_template}"
fi

if ! prompt="$(
  render_prompt_template "${selected_prompt_template}" \
    REPOSITORY_ROOT "${container_checkout_dir}" \
    BRANCH "${branch}" \
    REQUESTED_BASE_REF "${requested_base_ref:-不适用}" \
    REQUESTED_BASE_SHA "${requested_base_sha:-不适用}" \
    REQUESTED_HEAD_REF "${requested_head_ref:-不适用}" \
    REQUESTED_HEAD_SHA "${requested_head_sha:-不适用}" \
    BASE_SHA "${base_sha}" \
    TARGET_SHA "${target_sha}" \
    LOCAL_CI_STATUS "${local_ci_status}" \
    ANALYSIS_MODE "${analysis_mode}" \
    DIFF_MODE "${diff_mode}" \
    DIFF_COMMAND "${diff_command}" \
    CHANGE_REQUEST_CONTEXT_JSON "${change_request_context_json}" \
    REVIEW_CONTEXT_PROFILE "${review_context_profile}" \
    REVIEW_CONTEXT_HINT "${review_context_hint}" \
    CHANGED_FILE_COUNT "${changed_file_count}" \
    CHANGED_FILES_MANIFEST_JSON "${changed_files_manifest_json}" \
    CHANGED_FILE_GROUPS_JSON "${changed_file_groups_json}" \
    CHANGED_FILES_MANIFEST_PATH "${container_changed_files_manifest}" \
    LOCAL_CI_LOG "${container_local_ci_log}" \
    ARTIFACT_DIR "${artifact_dir:-未识别到具体目录}" \
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
  fail_ai_ci "无法渲染 Codex AI CI 提示词模板：${selected_prompt_template}"
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
    ' >> "${log_path}" 2>&1 &
codex_exec_pid=$!
startup_deadline=$((SECONDS + 10#${CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS}))
while kill -0 "${codex_exec_pid}" 2>/dev/null; do
  if grep -Eq '"type"[[:space:]]*:[[:space:]]*"(item\.(started|completed)|turn\.completed)"' \
    "${log_path}"; then
    startup_progress="true"
    break
  fi
  if ((SECONDS >= startup_deadline)); then
    startup_timed_out="true"
    echo "Codex AI CI 启动 watchdog：${CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS} 秒内未出现首个有效进展事件。" \
      >> "${log_path}"
    kill -ALRM "${codex_exec_pid}" 2>/dev/null || \
      kill -TERM "${codex_exec_pid}" 2>/dev/null || true
    break
  fi
  sleep 2
done
wait "${codex_exec_pid}"
exit_code=$?
if grep -Eq '"type"[[:space:]]*:[[:space:]]*"(item\.(started|completed)|turn\.completed)"' \
  "${log_path}"; then
  startup_progress="true"
fi
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

if len(generated_files) > max_files:
    reasons.append(
        f"生成测试文件数量 {len(generated_files)} 超过限制 {max_files}"
    )
if len(commands) > max_commands:
    reasons.append(
        f"测试、构建、lint 或诊断命令数量 {len(commands)} 超过限制 {max_commands}"
    )
if max_duration > recommended_timeout:
    reasons.append(
        f"单条命令最长耗时 {max_duration:g} 秒超过建议上限 "
        f"{recommended_timeout} 秒"
    )
if total_duration > test_budget:
    reasons.append(
        f"测试和诊断命令累计耗时 {total_duration:g} 秒超过建议预算 "
        f"{test_budget} 秒"
    )
if analysis_mode == "full" and test_generation_expected and not generated_files:
    reasons.append("可测试代码改动未生成测试文件，测试证据不足")
if analysis_mode == "full" and test_generation_expected and not commands:
    reasons.append(
        "可测试代码改动未记录测试、构建或 lint 命令，测试证据不足"
    )
constraint_status = "warning" if reasons else "pass"
constraint_reason = (
    "；".join(reasons)
    if reasons
    else "生成测试文件、执行测试或诊断命令的数量和耗时均在轻量约束范围内。"
)

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
    --head-sha "${requested_head_sha}"
    --tested-sha-kind "$([[ "${branch}" =~ ^ci/pr-[0-9]+/.+ ]] && printf '%s' pr_merge || printf '%s' commit)"
    --local-ci-status "${local_ci_status}"
    --changed-file-count "${changed_file_count}"
    --changed-files-manifest "${changed_files_manifest_path}"
    --repository-root "${workspace_dir}"
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

if [[ "${startup_timed_out}" == "true" ]]; then
  set_failure_reason "Codex 启动阶段超过 ${CODEX_AI_CI_STARTUP_TIMEOUT_SECONDS} 秒仍未出现首个有效进展"
elif [[ ${exit_code} -eq 124 || ${exit_code} -eq 137 ]]; then
  set_failure_reason "Codex 执行超过 ${CODEX_AI_CI_TIMEOUT_SECONDS} 秒硬超时"
elif [[ ${exit_code} -ne 0 ]]; then
  set_failure_reason "Codex exec 异常退出，退出码为 ${exit_code}"
elif [[ "${marker_found}" != "true" ]]; then
  set_failure_reason "结构化报告缺少 CODEX_AI_CI_COMPLETE 标记"
elif [[ "${report_format_valid}" != "true" ]]; then
  renderer_failure_tail="$(grep -F "Invalid Codex AI report:" "${log_path}" | tail -n 1 || true)"
  if [[ "${renderer_failure_tail}" == *".file"* || "${renderer_failure_tail}" == *".line"* || "${renderer_failure_tail}" == *"finding"* ]]; then
    failure_code="invalid_finding_location"
  fi
  set_failure_reason "结构化报告未通过 schema、固定格式或中文内容校验${renderer_failure_tail:+：${renderer_failure_tail}}"
elif [[ "${turn_completed}" != "true" ]]; then
  set_failure_reason "Codex JSONL 日志中没有 turn.completed 事件"
elif [[ "${command_executed}" != "true" ]]; then
  set_failure_reason "Codex 没有执行任何用于检查代码或日志的命令"
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

echo "Codex AI CI：失败（${failure_code:-codex_execution_failed}：${failure_reason}）"
exit 1
