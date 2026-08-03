#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
renderer="${repo_root}/scripts/local_ci/render_codex_ai_report.py"
test_root="$(mktemp -d /tmp/local-ci-codex-report-test.XXXXXX)"
trap 'rm -rf -- "${test_root}"' EXIT

valid_json="${test_root}/valid.json"
report_md="${test_root}/report.md"
comment_md="${test_root}/comment.md"
manifest_json="${test_root}/changed-files.json"
cat > "${manifest_json}" <<'JSON'
[
  {"path": "python/example.py", "change_type": "modified"}
]
JSON
cat > "${valid_json}" <<'JSON'
{
  "verdict": "WARNING",
  "summary": "发现一个可能引起行为回归的中风险问题。",
  "merge_recommendation": "建议修复缓存版本校验问题并重新运行定向测试后合入。",
  "changed_files": [
    {
      "path": "python/example.py",
      "change_type": "modified",
      "summary": "调整了缓存命中后的状态读取逻辑。",
      "impact": "可能影响版本变化后的缓存一致性。",
      "validation_strategy": "通过版本变化后的缓存失效用例验证。"
    }
  ],
  "behavior_coverage": {
    "normal": {
      "scope": "缓存版本一致时的正常命中路径。",
      "strategy": "执行现有缓存命中测试。",
      "result": "正常命中路径验证通过。"
    },
    "boundary": {
      "scope": "版本号刚好发生变化的边界路径。",
      "strategy": "生成版本变化后的缓存失效用例。",
      "result": "发现缓存没有及时失效。"
    },
    "error": {
      "scope": "缓存内容不可用时的错误路径。",
      "strategy": "检查异常处理分支和已有测试。",
      "result": "未发现新的错误处理问题。"
    },
    "compatibility": {
      "scope": "旧版本缓存记录的兼容读取路径。",
      "strategy": "检查版本字段缺失时的处理逻辑。",
      "result": "旧记录仍可按既有规则处理。"
    },
    "integration": {
      "scope": "缓存模块与调用方之间的集成路径。",
      "strategy": "执行调用方定向回归测试。",
      "result": "除版本失配路径外未发现集成回归。"
    }
  },
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
  --changed-files-manifest "${manifest_json}" \
  --constraint-status warning \
  --constraint-reason "测试命令数量超过轻量约束。")"
[[ "${verdict}" == "WARNING" ]]

grep -Fq "# Codex AI CI 报告" "${report_md}"
grep -Fq "## 元数据" "${report_md}"
grep -Fq "## 结论" "${report_md}"
grep -Fq "**警告**" "${report_md}"
grep -Fq 'triton-anchor-codex-ai-report/v2' "${report_md}"
grep -Fq "## 合入建议" "${report_md}"
grep -Fq "## 具体文件变更" "${report_md}"
grep -Fq "## 行为覆盖" "${report_md}"
grep -Fq "## 关键问题" "${report_md}"
grep -Fq "缓存命中后返回了过期状态" "${report_md}"
grep -Fq "## 建议测试" "${report_md}"
grep -Fq "## 测试执行" "${report_md}"
grep -Fq "## 测试执行约束" "${report_md}"
grep -Fq "状态：警告" "${report_md}"
grep -Fq "测试命令数量超过轻量约束" "${report_md}"
grep -Fq "## 剩余风险" "${report_md}"
grep -Fq "## Codex AI 审核摘要" "${comment_md}"
grep -Fq "合入建议：**" "${comment_md}"
grep -Fq "### 补充验证" "${comment_md}"
grep -Fq "### 需要重点关注的问题" "${comment_md}"
grep -Fq "### 具体文件变更" "${comment_md}"
grep -Fq "<details>" "${comment_md}"
grep -Fq "约束提醒：" "${comment_md}"
grep -Fq "测试命令数量超过轻量约束" "${comment_md}"
if grep -Fq "新分支直接复用缓存值" "${comment_md}"; then
  echo "PR 评论不应包含 finding 的完整证据" >&2
  exit 1
fi
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
  --changed-files-manifest "${manifest_json}" \
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
  --changed-files-manifest "${manifest_json}" \
  >/dev/null 2>&1; then
  echo "渲染器接受了英文说明性字段" >&2
  exit 1
fi

wrong_file_report="${test_root}/wrong-file.json"
sed 's#"path": "python/example.py"#"path": "python/other.py"#' \
  "${valid_json}" > "${wrong_file_report}"
if python3 "${renderer}" \
  --input "${wrong_file_report}" \
  --output "${test_root}/wrong-file.md" \
  --comment-output "${test_root}/wrong-file-comment.md" \
  --branch test --base-sha a --target-sha b --changed-file-count 1 \
  --changed-files-manifest "${manifest_json}" \
  >/dev/null 2>&1; then
  echo "渲染器接受了与 Git diff 清单不一致的 changed_files" >&2
  exit 1
fi

duplicate_file_report="${test_root}/duplicate-file.json"
python3 - "${valid_json}" "${duplicate_file_report}" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
document["changed_files"].append(dict(document["changed_files"][0]))
Path(sys.argv[2]).write_text(
    json.dumps(document, ensure_ascii=False), encoding="utf-8"
)
PY
if python3 "${renderer}" \
  --input "${duplicate_file_report}" \
  --output "${test_root}/duplicate-file.md" \
  --comment-output "${test_root}/duplicate-file-comment.md" \
  --branch test --base-sha a --target-sha b --changed-file-count 1 \
  --changed-files-manifest "${manifest_json}" \
  >/dev/null 2>&1; then
  echo "渲染器接受了重复的 changed_files 条目" >&2
  exit 1
fi

missing_behavior_report="${test_root}/missing-behavior.json"
python3 - "${valid_json}" "${missing_behavior_report}" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
del document["behavior_coverage"]["integration"]
Path(sys.argv[2]).write_text(
    json.dumps(document, ensure_ascii=False), encoding="utf-8"
)
PY
if python3 "${renderer}" \
  --input "${missing_behavior_report}" \
  --output "${test_root}/missing-behavior.md" \
  --comment-output "${test_root}/missing-behavior-comment.md" \
  --branch test --base-sha a --target-sha b --changed-file-count 1 \
  --changed-files-manifest "${manifest_json}" \
  >/dev/null 2>&1; then
  echo "渲染器接受了缺少集成路径的 behavior_coverage" >&2
  exit 1
fi

echo "Codex AI 中文报告格式与内容校验：通过"
