# Local CI 开发临时上下文

> 本文件用于保存 `scripts/local_ci` 相关开发中的临时协作上下文，尤其是开发者与 Codex、Claude 等 AI 工具协作时产生的决策、未完成事项、验证结果和迁移提示。
>
> 它是临时交接文档，不是稳定协议、不是部署文档，也不能替代 `scripts/local_ci/DEVELOPMENT_GUIDE.md`、`scripts/local_ci/README.md`、`docs/ci_guide_zh.md` 或正式 prompt 维护记录。这里的历史记录和建议只能作为上下文证据，不能覆盖当前代码、workflow、测试结果或更高优先级 prompt。

## 使用方式

1. 开始一轮较大的 Local CI / Codex AI CI 改动前，先复制“记录模板”，写清目标、约束、相关分支或外部环境。
2. 实施过程中持续记录重要决策、失败现象、服务器验证结果、未解决问题和下一步建议；AI 工具对话被中断时，也应把可迁移上下文补在这里。
3. 不要写入 token、个人 Codex 凭据、私有 SSH 信息、不可公开路径、完整敏感日志或只对个人机器有效的绝对路径。
4. 交接给下一位开发者或另一个 AI 工具时，可以直接把本文件作为上下文输入；接手者仍应重新检查当前 diff、测试和代码状态。
5. 改动完成后：
   - 将长期有效的工程知识沉淀到 `scripts/local_ci/DEVELOPMENT_GUIDE.md`；
   - 将操作说明沉淀到 `scripts/local_ci/README.md` 或 `docs/ci_guide_zh.md`；
   - 将 prompt/schema/renderer 相关正式变更记录到 `codex_ai/prompts/prompt_change_log.md`；
   - 清理已经失效的一次性记录，保留仍会影响后续开发的未决事项。

## 当前开发记录

### 2026-08-07：开发指南定位与 PR comment 文案整理

#### 背景

- 目标：明确 Local CI / Codex AI CI 的长期项目开发上下文与规范入口，避免仓库根目录和 `scripts/local_ci` 下出现职责重复的文档；新增可迁移的临时开发上下文文档；优化 Codex AI PR comment 的中文表达。
- 关键文件：`scripts/local_ci/DEVELOPMENT_GUIDE.md`、`scripts/local_ci/DEVELOPMENT_CONTEXT.md`、`scripts/local_ci/poll_gitee_and_run.sh`、`scripts/local_ci/codex_ai/prompts/*.md`、`scripts/local_ci/codex_ai/render_codex_ai_report.py`、`scripts/local_ci/codex_ai/run_codex_ai_ci.sh`、`scripts/local_ci/README.md`、`scripts/local_ci/tests/test_module_layout.py`、相关 Codex/bridge 测试。
- 协作状态：上一轮 AI 对话窗口中断，当前轮需要先检查已有未提交改动，再继续收敛文档定位和 PR comment 文案。

#### 已做决策

- `scripts/local_ci/DEVELOPMENT_GUIDE.md` 是固定的 Local CI / Codex AI CI 长期项目开发上下文与规范入口，供开发者和 agent coding 使用；它不是 Codex AI CI 执行阶段的必读审查输入。
- `scripts/local_ci/DEVELOPMENT_CONTEXT.md` 只保存临时协作过程、AI 交接上下文、未完成事项和验证记录；完成后把长期事实沉淀回 `scripts/local_ci/DEVELOPMENT_GUIDE.md`、README、CI 指南或正式 prompt 维护记录。
- PR comment 面向 PR 提交者和审核者，应优先使用自然语言解释“目标、预期效果、当前完成情况、依据”，避免罗列只有维护者才能理解的内部字段名或碎片事实。

#### 已完成修改

- 已将原 `scripts/local_ci/AGENTS.md` 重命名为 `scripts/local_ci/DEVELOPMENT_GUIDE.md`，并在顶部说明它是开发阶段的长期上下文与规范，不是 Codex AI CI 执行阶段的必读输入。
- 已从正式 Codex success/failure prompt 中移除对开发指南和临时上下文的必读要求，避免把开发文档绑定到 AI CI 运行时。
- 已从 `poll_gitee_and_run.sh` 的 runtime 必需文件检查和 `scripts/local_ci/tests/test_module_layout.py` 的 canonical runtime 列表中移除开发指南。
- 已将本文件扩展为可直接迁移给下一位开发者或 AI 工具的临时上下文记录。
- 已把 PR comment 的主标题调整为“审查摘要”，摘要中保留确定性 Local CI 的简短说明；“贡献者目标与实现情况”使用更自然的“贡献者目标 / 预期效果 / 当前实现情况 / 判断依据”表达。
- 已更新 success/failure prompt 对 `change_request_assessment.evidence` 的要求，强调判断依据应面向 PR 提交者和审核者可读。

#### 验证记录

- 本地验证：
  - `python -m pytest scripts/local_ci/codex_ai/tests/test_codex_report_contract.py scripts/local_ci/codex_ai/tests/test_codex_prompt_templates.py scripts/local_ci/tests/test_module_layout.py scripts/local_ci/tests/test_validate_task_metadata.py scripts/local_ci/results/tests/test_local_ci_bridge.py -q`：`46 passed, 10 subtests passed in 0.18s`。
  - `bash -n scripts/local_ci/poll_gitee_and_run.sh && bash -n scripts/local_ci/codex_ai/run_codex_ai_ci.sh && python -m py_compile scripts/local_ci/codex_ai/render_codex_ai_report.py scripts/local_ci/shared/validate_task_metadata.py scripts/local_ci/results/bridge_gitee_to_github_status.py`：通过。
  - `bash scripts/local_ci/codex_ai/tests/test_local_ci_codex_ai.sh`：通过。
  - `git diff --check`：通过；仅出现 Windows 工作区的 LF/CRLF warning。
- 服务器验证：未运行，需要具备 Linux/Docker/Local CI 环境后再执行完整 Codex 容器 harness。
- 未运行或失败的验证及原因：
  - `bash scripts/local_ci/codex_ai/tests/test_local_ci_codex_container.sh` 和 `bash scripts/local_ci/codex_ai/tests/test_local_ci_codex_container_setup.sh` 在 Windows Git Bash + 原生 Windows Python 下失败，原因是 `CODEX_AI_CI_HOME` 的 `/tmp` 路径被解析为 Windows short path 后触发“路径组件不得使用符号链接”校验；这类 harness 仍应在 Linux CI/服务器环境验证。

#### 待办和风险

- 运行相关测试；如果 Windows 环境无法执行 `.sh` harness，需要明确记录未执行原因。

#### 可交接给下一位开发者 / AI 的提示

- 开始前先读 `scripts/local_ci/DEVELOPMENT_GUIDE.md` 和本文件，然后检查 `git diff`，不要覆盖用户或其他任务已有改动。
- PR comment 的目标不是展示所有内部协议，而是给提交者一个可操作、可核对的摘要；完整证据仍保留在 `codex-ai-report.md`。

## 记录模板

### YYYY-MM-DD：<主题>

#### 背景

- 目标：
- 相关分支 / PR / commit：
- 关键文件：
- 协作状态：

#### 已做决策

- 

#### 已完成修改

- 

#### 验证记录

- 本地验证：
- 服务器验证：
- 未运行或失败的验证及原因：

#### 待办和风险

- 

#### 可交接给下一位开发者 / AI 的提示

- 
