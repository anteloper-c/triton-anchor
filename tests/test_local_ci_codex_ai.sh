#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
renderer="${repo_root}/scripts/local_ci/render_codex_ai_report.py"
test_root="$(mktemp -d /tmp/local-ci-codex-report-test.XXXXXX)"
trap 'rm -rf -- "${test_root}"' EXIT

valid_json="${test_root}/valid.json"
report_md="${test_root}/report.md"
comment_md="${test_root}/comment.md"
cat > "${valid_json}" <<'JSON'
{
  "verdict": "WARNING",
  "summary": "发现一个可能引起行为回归的中风险问题。",
  "findings": [
    {
      "id": "AI-001",
      "severity": "MEDIUM",
      "category": "regression",
      "file": "python/example.py",
      "line": "17",
      "title": "缓存命中后返回了过期状态",
      "evidence": "新分支直接复用缓存值，但没有核对当前版本号。",
      "impact": "调用方可能读取到上一次任务遗留的状态。",
      "fix_direction": "在复用缓存前校验版本号，并为失配路径补充测试。"
    }
  ],
  "suggested_tests": [
    {
      "id": "TEST-001",
      "priority": "MEDIUM",
      "target": "python/tests/test_example.py",
      "description": "增加版本变化后缓存必须失效的回归测试。"
    }
  ],
  "residual_risks": [
    "本次只执行了定向测试，尚未覆盖并发更新场景。"
  ],
  "test_execution": {
    "status": "passed",
    "summary": "生成的缓存失效测试已经通过。",
    "generated_test_files": [
      "python/tests/test_generated_cache.py"
    ],
    "commands": [
      {
        "id": "RUN-001",
        "command": "python3 -m pytest python/tests/test_generated_cache.py",
        "exit_code": 0,
        "duration_seconds": 0.2,
        "status": "passed",
        "evidence": "定向测试共执行一个用例并通过。"
      }
    ]
  },
  "completion_marker": "CODEX_AI_CI_COMPLETE"
}
JSON

verdict="$(python3 "${renderer}" \
  --input "${valid_json}" \
  --output "${report_md}" \
  --comment-output "${comment_md}" \
  --branch "ci/push/ai-ci" \
  --base-sha "$(printf 'a%.0s' {1..40})" \
  --target-sha "$(printf 'b%.0s' {1..40})" \
  --changed-file-count 1 \
  --constraint-status warning \
  --constraint-reason "测试命令数量超过轻量约束。")"
[[ "${verdict}" == "WARNING" ]]

grep -Fq "# Codex AI CI 报告" "${report_md}"
grep -Fq "## 元数据" "${report_md}"
grep -Fq "## 结论" "${report_md}"
grep -Fq "**警告**" "${report_md}"
grep -Fq "## 关键问题" "${report_md}"
grep -Fq "缓存命中后返回了过期状态" "${report_md}"
grep -Fq "## 建议测试" "${report_md}"
grep -Fq "## 测试执行" "${report_md}"
grep -Fq "## 测试执行约束" "${report_md}"
grep -Fq "状态：警告" "${report_md}"
grep -Fq "测试命令数量超过轻量约束" "${report_md}"
grep -Fq "## 剩余风险" "${report_md}"
grep -Fq "## Codex AI 关键问题概述" "${comment_md}"
grep -Fq "### 测试执行约束警告" "${comment_md}"
grep -Fq "测试命令数量超过轻量约束" "${comment_md}"
if grep -Eq '^## (Metadata|Verdict|Summary|Findings|Suggested Tests|Test Execution|Residual Risks|Execution)$' "${report_md}"; then
  echo "报告仍包含英文模板标题" >&2
  exit 1
fi
python3 -c 'from pathlib import Path; Path("'"${report_md}"'").read_text(encoding="utf-8"); Path("'"${comment_md}"'").read_text(encoding="utf-8")'

invalid_verdict="${test_root}/invalid-verdict.json"
sed 's/"verdict": "WARNING"/"verdict": "PASS"/' "${valid_json}" > "${invalid_verdict}"
if python3 "${renderer}" \
  --input "${invalid_verdict}" \
  --output "${test_root}/invalid-verdict.md" \
  --comment-output "${test_root}/invalid-verdict-comment.md" \
  --branch test --base-sha a --target-sha b --changed-file-count 1 \
  >/dev/null 2>&1; then
  echo "渲染器接受了与 findings 不一致的 verdict" >&2
  exit 1
fi

english_report="${test_root}/english.json"
sed 's/发现一个可能引起行为回归的中风险问题。/English-only summary./' \
  "${valid_json}" > "${english_report}"
if python3 "${renderer}" \
  --input "${english_report}" \
  --output "${test_root}/english.md" \
  --comment-output "${test_root}/english-comment.md" \
  --branch test --base-sha a --target-sha b --changed-file-count 1 \
  >/dev/null 2>&1; then
  echo "渲染器接受了英文说明性字段" >&2
  exit 1
fi

echo "Codex AI 中文报告格式与内容校验：通过"