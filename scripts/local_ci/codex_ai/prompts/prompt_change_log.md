# Codex AI Prompt 维护记录

本文是 `scripts/local_ci/codex_ai/prompts/` 下 Codex AI CI prompt 的长期维护记录，不只记录某一次修改。后续凡是调整正式 prompt、prompt 变量、输出契约、审查策略、验证约束或相关模板测试，都应在本文追加一条记录，说明修改动机、影响范围和验证方式。

## 维护范围

当前正式 prompt 包括：

- `codex_ai_success.md`：确定性 Local CI 成功后的补充审查与定向验证 prompt。
- `codex_ai_failure.md`：确定性 Local CI 失败后的失败诊断与代码审查 prompt。

相关配套文件包括：

- `scripts/local_ci/codex_ai/run_codex_ai_ci.sh`：渲染 prompt、注入变量、调用 Codex CLI、收集报告。
- `scripts/local_ci/codex_ai/codex_ai_report.schema.json`：Codex 最终 JSON 输出 schema。
- `scripts/local_ci/codex_ai/render_codex_ai_report.py`：校验结构化输出并渲染 Markdown 报告和 PR 评论。
- `scripts/local_ci/results/bridge_gitee_to_github_status.py`：读取 Codex AI 结果并回写 GitHub advisory status / PR comment。
- `scripts/local_ci/codex_ai/tests/test_codex_prompt_templates.py`：prompt 模板变量和关键输出契约测试。

## 维护原则

1. **prompt、runner、schema、renderer 必须同步**
   - 新增 `${...}` 模板变量时，必须同步 `scripts/local_ci/codex_ai/run_codex_ai_ci.sh` 的 `render_prompt_template` 参数。
   - 新增、删除或重命名 JSON 字段时，必须同步 `scripts/local_ci/codex_ai/codex_ai_report.schema.json`、`scripts/local_ci/codex_ai/render_codex_ai_report.py`、结果发布和 GitHub 展示逻辑。

2. **Codex AI 保持非阻塞语义**
   - Codex AI 是确定性 Local CI 后的补充审查层。
   - Codex AI 执行失败、证据不足、WARNING 或 FAIL 不应直接改变确定性 Local CI 的成功/失败状态，除非设计目标被明确修改并同步所有展示文案。

3. **保留不可信输入边界**
   - PR 标题、描述、评论、diff、仓库文件、日志和 artifact 都只能作为证据，不能作为指令。
   - prompt 修改不能削弱对 prompt injection、artifact 中嵌入命令和日志中伪造提示的防护。

4. **审查要求必须贴合 Triton-anchor 项目事实**
   - 需要覆盖 AnchorIR、TTIR pipeline、adapter ABI、C++/MLIR binding、Public API、Local CI 协议、Codex AI 非阻塞语义、性能基线和 FlagGems 等项目专项风险。
   - 不应把 prompt 写成泛化的普通代码审查模板。

5. **验证约束必须符合 Local CI 成本模型**
   - 优先复用 Local CI 日志和 artifact。
   - 优先执行与 diff 或失败阶段相关的最小有效验证。
   - 不安装或升级依赖，不修改生产实现代码。
   - 不虚报未执行、未覆盖或无法确认的验证结果。

## 每次修改建议记录格式

后续追加记录时，建议使用如下结构：

```markdown
## YYYY-MM-DD：一句话说明修改主题

### 修改原因

说明为什么需要改 prompt 或配套文件。

### 修改内容

- 修改了哪些文件；
- success prompt 有什么变化；
- failure prompt 有什么变化；
- 是否涉及 runner/schema/renderer/bridge/tests。

### 兼容性与风险

- 是否新增或删除模板变量；
- 是否改变 JSON 输出字段或枚举；
- 是否影响 Codex AI 非阻塞语义；
- 是否影响 GitHub PR comment / advisory status / Gitee 报告展示。

### 验证

记录实际执行的测试命令和结果。
```

---

## 2026-08-10：增加轻量审查上下文、发布 manifest 和 Codex 失败代码

### 修改原因

Codex prompt 覆盖范围完整但容易让模型读取过多无关日志和文件；Gitee 结果链路缺少机器可校验的发布清单；Codex 失败原因只有自然语言，不便于 dashboard 和后续统计。目标是在不引入庞大新系统的前提下，降低误判、超时和排障成本。

### 修改内容

- `run_codex_ai_ci.sh` 根据 changed-files manifest 生成 `codex-context-summary.json` 和 review context profile，区分 `docs_only`、`codex_ai_ci_maintenance`、`local_ci_protocol`、`performance`、`local_ci_control`、`local_ci_failure`、`large_diff` 等轻量策略。
- success/failure prompt 新增动态上下文段落，要求 Codex 按 profile 优先读取相关文件、失败阶段日志和 artifact，但仍必须覆盖全部 changed-files manifest。其中 `codex_ai_ci_maintenance` 表示仅涉及 Codex AI-CI 自身维护文件，不纳入 triton-anchor 产品代码审查，不应生成产品 finding；同步性依赖专用契约测试和人工维护审查。
- `run_codex_ai_ci.sh` 将失败原因映射为固定 `failure_code`，例如 `codex_cli_unavailable`、`credential_validation_failed`、`container_setup_failed`、`prompt_render_failed`、`timeout`、`schema_validation_failed`、`missing_turn_completed`、`no_command_executed` 和 `invalid_finding_location`。
- `publish_gitee_result.py` 新增 `publish-manifest.json`，记录 copied files、缺失预期 artifact、target/tested SHA、run ID 和 fallback 状态；`latest.txt` 改为临时文件替换写入，结果分支 push 增加 fetch/rebase retry。
- `bridge_gitee_to_github_status.py` 读取结果时校验 manifest、summary 和 `result.json` 中的 SHA/run ID，并在 PR comment / advisory status / GitHub output 中暴露 Codex 执行状态、测试证据状态、失败代码和 artifact manifest warning。
- 同步更新 Local CI README、桌面说明文档和相关契约测试。

### 兼容性与风险

- Codex 报告 schema 仍为 `triton-anchor-codex-ai-report/v3`，没有新增模型输出字段；动态上下文只影响阅读和验证优先级。Codex AI-CI 自身维护变更不再作为 triton-anchor 产品审查对象。
- `publish-manifest.json` 是新增结果文件；旧结果没有该文件时 bridge 仍可读取，存在该文件但校验失败时会保持 pending，避免回写错误 SHA/run 的结果。
- Codex advisory 仍保持非阻塞；失败代码只用于展示和统计，不改变确定性 Local CI exit code。
- push retry 只做有限 fetch/rebase，不做强制 push；遇到无法 rebase 的冲突会失败并等待下一轮重新发布。

### 验证

已运行 `python -m py_compile scripts/local_ci/results/publish_gitee_result.py scripts/local_ci/results/bridge_gitee_to_github_status.py scripts/local_ci/codex_ai/render_codex_ai_report.py`、`bash -n scripts/local_ci/codex_ai/run_codex_ai_ci.sh && bash -n scripts/local_ci/poll_gitee_and_run.sh && bash -n scripts/local_ci/deterministic_ci/run_deterministic_ci.sh`、`python -m pytest scripts/local_ci/codex_ai/tests scripts/local_ci/tests scripts/local_ci/results/tests -q`（46 passed, 10 subtests passed）、`python -m compileall -q scripts/local_ci`、`bash scripts/local_ci/codex_ai/tests/test_local_ci_codex_ai.sh` 和 `git diff --check`。完整 Codex fake Docker harness 在当前 Windows Git Bash 环境因 `/tmp` 路径被解析为 Windows short path 后触发凭据路径组件 symlink 校验而失败，仍需在 Linux CI/服务器环境验证。

---

## 2026-08-10：统一 Local CI 文档与契约测试

### 修改原因

本次调整主要是把 Local CI 的使用说明、维护说明和契约测试口径重新收敛到单一路径，减少根目录与子目录文档之间的差异。

### 修改内容

- 更新根目录 README 的 Local CI 入口说明；
- 统一 `scripts/local_ci/README.md`、`DEVELOPMENT_GUIDE.md`、`docs/ci_guide_zh.md` 的长期说明；
- 调整相关契约测试，让文档、脚本和测试保持同一套口径。

### 兼容性与风险

- 现有 Local CI 流程保持不变；
- 后续如再补充新能力，需要同步更新根目录文档和 Local CI 文档。

### 验证

计划运行 `bash -n scripts/local_ci/codex_ai/run_codex_ai_ci.sh scripts/local_ci/codex_ai/setup_codex_ai_container.sh`、契约测试和文档检查。

---

## 2026-08-06：补充贡献者目标对照并统一可读 PR comment

### 修改原因

原报告虽然包含摘要、finding 和测试证据，但没有结构化表达“贡献者声称要完成什么、预期行为是什么、当前 diff 实际实现到什么程度”。PR comment 也把结论、验证和文件明细混在一起，普通开发者需要阅读完整报告才能理解合入影响。

### 修改内容

- 将 Codex 报告格式升级为 `triton-anchor-codex-ai-report/v3`。
- 新增 `change_request_assessment`，包含 `status`、`contributor_goal`、`expected_behavior`、`implementation_summary` 和 `evidence`。
- success/failure prompt 明确五种实现状态及其与 `verdict`、finding 的关系。
- renderer 和失败 fallback 同步校验、生成并展示该章节；PR comment 改为“审查摘要、贡献者目标与实现情况、需要处理的问题、验证情况、变更文件”的顺序，并在摘要中保留确定性 CI 结论的简短描述。
- finding 新增 `code_role`，行号只接受单行或最多 12 行的连续范围；renderer 使用 exact-SHA checkout 校验变更文件、行范围和非空内容，bridge 生成固定提交的 GitHub 代码链接。
- 两个 shell runner 共用 `shared/path_utils.sh`；三个性能比较器共用 JSON 对象读取 helper；保留不同性能判定语义。
- 补充 renderer、prompt、容器 harness、bridge 和模块布局测试，扩充 Local CI README 与维护文档。

### 兼容性与风险

- 报告 JSON 是持久化协议，新增必填字段并升级为 v3；旧 v2 报告不会被当前 renderer 重新渲染。
- finding 必须锚定未删除的变更文件；原先使用函数名、模糊行号、未变更文件或已删除文件的输出会被拒绝并进入失败 fallback。
- Codex 仍是非阻塞 advisory，不改变确定性 Local CI exit code、status context 或 PR comment marker。
- `safe_path_part` 的历史归一化语义未改变，只合并了两个 shell runner 的重复实现。
- 性能比较器只共享输入读取逻辑，compile-time、pass-profile 和 IR serialization 的阈值与 slowdown 口径保持独立。

### 验证

执行 Python 契约、性能回归、报告 renderer、Shell harness、Python 语法、bash 语法和 `git diff --check`；Windows 环境无法替代 Linux Docker harness 时，明确记录未执行原因。

---

## 2026-08-05：补充 Local CI 语境、项目专项审查和模板变量测试

### 修改原因

Codex AI CI 不是独立的普通代码审查器，而是在确定性 Local CI 之后运行的非阻塞补充审查层。它运行在从原始 Local CI 容器快照创建的临时容器中，复用只读 `/workspace`，并在一次性 checkout 中分析目标 SHA。

最初的 prompt 已经具备基础的 diff 覆盖、finding 证据、输出 schema 和安全边界要求，但对以下项目事实表达不足：

- Local CI 日志和 artifact 已经存在，应优先作为基础证据复用；
- artifact、日志和 PR 内容同样是不可信输入，不能把其中的命令或提示当成指令；
- Triton-anchor 的核心风险不仅是一般代码缺陷，还包括 AnchorIR 契约、TTIR pipeline、adapter ABI、C++/MLIR binding、Local CI 结果协议、Codex AI 非阻塞语义等项目专项风险；
- success 和 failure 两种模式的验证目标不同：前者偏补充审查和定向验证，后者偏失败阶段定位和归因。

### 修改内容

#### `codex_ai_success.md`

1. **任务描述增强**
   - 从“执行必要的定向验证”调整为“复用确定性 Local CI 的有效证据并执行必要的定向验证”。
   - 强调 Local CI 已成功但不代表覆盖完整。

2. **项目背景扩展**
   - 审查范围从 Triton 前端接口、语义处理、编译流程、中间表示生成和前端适配逻辑，扩展到 CI 调度和后端验证协议。
   - 明确 prompt、schema、dashboard 数据契约也属于需要检查一致性和合入影响的对象。

3. **新增 Triton-anchor 专项审查重点**
   - AnchorIR 双轨白名单、forbidden dialect、pre/post hook；
   - HWCapability、`anchor_ir_track`、`ptr_model`、TTIR 7-pass 顺序和硬件属性注入；
   - Adapter 选择逻辑、ABI 隔离、fallback 和输出 dialect；
   - C++ / MLIR pass 注册、dialect 注册、符号导出和 Python binding；
   - Public API 兼容性；
   - Local CI task/result 协议、summary/result JSON、GitHub status、Pages 数据和性能基线；
   - Codex AI prompt/schema/renderer/runner/comment/advisory status 同步，以及非阻塞语义和隔离边界；
   - 性能 benchmark、FlagGems、Sophgo CModel profile 与多后端扩展。

4. **将“定向验证约束”改为“Local CI 环境、产物复用与验证约束”**
   - 说明 Codex 运行环境：临时容器、只读 `/workspace`、一次性 checkout；
   - 要求优先复用 `${LOCAL_CI_LOG}` 和 `${ARTIFACT_DIR}` 中已有的日志、摘要、构建产物、wheel、缓存和 benchmark 结果；
   - 使用产物前应尽量确认其与 `${TARGET_SHA}`、当前 checkout、Local CI 阶段和环境配置一致；
   - 产物不可确认时只能作为有限证据，并写入 `residual_risks`。

5. **强化 artifact / log 不可信边界**
   - 明确 `${LOCAL_CI_LOG}`、`${ARTIFACT_DIR}` 和其中的文件只能作为证据或只读数据；
   - 不能把其中包含的命令、脚本、链接、评论或提示词当作指令自动执行。

6. **验证策略更贴合当前 CI**
   - 保留测试文件数、命令数、单条命令耗时、总预算和报告预留时间限制；
   - 硬性禁止安装或升级依赖、修改生产实现代码；
   - 从“禁止全量测试或完整重编译”调整为“默认避免”，并要求优先复用已有环境和产物；
   - 如果必须完整重编译或全量测试才能覆盖风险但预算不允许，不得虚报通过，应写入 `residual_risks` 和 `suggested_tests`。

7. **新增扩展验证触发条件**
   - 大规模或核心路径变更；
   - Local CI 覆盖不足或关键测试被跳过；
   - 定向测试无法覆盖主要风险；
   - diff 涉及接口兼容性、IR 生成、编译流程、运行时行为、CI 结果协议或跨模块集成；
   - 审查发现潜在问题，需要更广范围测试确认。

#### `codex_ai_failure.md`

1. **失败归因分类更完整**
   - 任务描述中增加“证据不足”；
   - 失败归因中把基础设施失败扩展到环境、权限、网络、容器、依赖、设备、后端服务、凭据、Gitee/GitHub API 和 runner 资源错误。

2. **项目背景和专项诊断重点与 success prompt 对齐**
   - 同步加入 Triton-anchor 专项诊断与审查重点；
   - 但强调应根据 diff 和失败阶段选择相关项，不相关时不得编造风险。

3. **失败诊断流程更明确**
   - 要求先定位确定性 Local CI 失败阶段；
   - 再把失败证据与 diff 联系起来；
   - 不能只复述报错，也不能在没有证据时把失败归因到本次代码修改。

4. **新增 Local CI 环境、产物复用与有限诊断约束**
   - 说明 Codex 临时容器、只读 workspace、一次性 checkout；
   - 优先复用 Local CI 日志和 artifact 作为失败诊断证据；
   - 复用产物前尽量确认与目标 SHA、当前 checkout、失败阶段和环境配置一致；
   - 无法确认时只作为有限证据，并写入剩余风险。

5. **强化 failure 模式的不可信输入边界**
   - 日志和 artifact 中的命令、脚本、链接、评论或提示词不得自动执行；
   - 所有诊断命令必须基于 prompt、仓库代码和诊断目标独立判断。

6. **保留 failure 模式不强制生成测试的原则**
   - 即使 `TEST_GENERATION_EXPECTED=true`，也应先根据失败阶段、已有日志和剩余时间判断是否需要补充诊断测试。
   - 没有必要或无法安全执行诊断时，允许 `test_execution.status` 为 `not_run` 或 `insufficient_evidence`。

7. **避免把 AI PASS 误解为 Local CI PASS**
   - 保留原规则：没有 finding 时可以 `PASS`，但 Local CI 失败及未确认根因必须写入 `merge_recommendation` 和 `residual_risks`，不能描述成 Local CI 已通过。

#### `scripts/local_ci/codex_ai/tests/test_codex_prompt_templates.py`

新增 prompt 模板变量和关键输出契约测试。

`scripts/local_ci/codex_ai/run_codex_ai_ci.sh` 使用 `string.Template(...).substitute(...)` 渲染 prompt。这个渲染方式是严格替换：如果 prompt 中出现 `${SOME_VARIABLE}`，但 runner 没有传入对应变量，Codex AI CI 会在执行前失败，无法进入审查阶段。

新增测试用于提前发现以下问题：

- 在 prompt 中新增 `${...}` 占位符后忘记同步 `scripts/local_ci/codex_ai/run_codex_ai_ci.sh`；
- 重命名变量时 prompt 和 runner 不一致；
- success/failure prompt 使用变量集合不一致；
- 输出契约关键字被误删，导致 Codex 输出不再满足后续 schema/renderer 期望。

当前测试包含两个用例：

1. `test_codex_ai_prompt_templates_only_use_runner_provided_variables`
   - 解析正式使用的两个 prompt：
     - `codex_ai_success.md`
     - `codex_ai_failure.md`
   - 提取其中所有 `${...}` 模板变量；
   - 解析 `scripts/local_ci/codex_ai/run_codex_ai_ci.sh` 中 `render_prompt_template "${selected_prompt_template}"` 调用传入的变量名；
   - 断言 prompt 使用的变量都由 runner 提供。

2. `test_codex_ai_prompt_templates_keep_required_output_contract`
   - 检查两个正式 prompt 中仍保留关键输出契约文本：
     - `triton-anchor-codex-ai-report/v2`
     - `CODEX_AI_CI_COMPLETE`
     - `changed_files`
     - `behavior_coverage`
   - 防止后续改 prompt 时误删 schema、completion marker 或核心字段要求。

### 兼容性与风险

- 本次未新增 JSON 输出字段，未修改 `scripts/local_ci/codex_ai/codex_ai_report.schema.json`。
- 本次未新增 runner 未提供的模板变量。
- 本次未改变 Codex AI 非阻塞语义。
- 本次不影响 Gitee result path、GitHub status context 或 PR comment marker。
- prompt 内容变长，可能增加少量 Codex 输入 token，但换取更明确的审查边界和更少的误判风险。

### 验证

使用 `python` 执行：

```bash
PYTHONPATH=python python -m pytest scripts/local_ci/codex_ai/tests/test_codex_prompt_templates.py -v --tb=short
```

结果：

```text
2 passed in 0.06s
```

---

## 2026-08-05：将 Codex prompt 模板测试移入 Codex AI 模块

### 修改原因

prompt 模板测试属于 `scripts/local_ci/codex_ai` 的配套契约测试，不应继续放在 `python/triton_anchor/tests` 中，以免和 `triton_anchor` 包内测试职责混淆。

### 修改内容

- 将 `test_codex_prompt_templates.py` 从 `python/triton_anchor/tests/` 移到 `scripts/local_ci/codex_ai/tests/`。
- 更新本文中的测试路径和验证命令。

### 兼容性与风险

- 不改变 prompt、runner、schema、renderer 或 bridge 的行为。
- 仅调整测试归属，不影响运行时产物。

### 验证

```bash
PYTHONPATH=python python -m pytest scripts/local_ci/codex_ai/tests/test_codex_prompt_templates.py -q
```

结果：`2 passed in 0.02s`

---

## 2026-08-05：按职责拆分 Local CI 与 Codex AI 目录

> 本节记录当时的过渡方案。该方案已被 2026-08-06 的最终目录收敛取代：根目录只保留完整实现的 `poll_gitee_and_run.sh`，其他顶层兼容入口均已删除。

### 修改原因

原 `scripts/local_ci/` 同时放置确定性 CI、Codex AI、性能测试、结果发布和共享协议文件，调用边界不直观，也容易让新增测试和文档引用尚未落地的目录结构。

### 修改内容

- 将任务轮询和容器调度迁入 `orchestration/`；
- 将确定性 delivery、FlagGems 和性能脚本迁入 `deterministic_ci/`；
- 将 Codex runner、prompt、schema、renderer、凭据校验和测试迁入 `codex_ai/`；
- 将结果发布和 GitHub bridge 迁入 `results/`，共享路径和 metadata 协议迁入 `shared/`；
- 保留原顶层脚本作为已部署命令的兼容入口，canonical 实现只保留在分层目录；
- 新增模块布局契约测试，并更新 workflow、测试、配置模板和维护文档中的 canonical 路径。

### 兼容性与风险

- 不改变 task ref、结果目录、artifact 名称、报告 schema、状态 context 或性能基线协议；
- `LOCAL_CI_SCRIPT_DIR` 继续指向完整的 `scripts/local_ci` 根目录，runner snapshot 会包含所有模块和兼容入口；
- 确定性 CI 仍先于 Codex AI 执行，Codex 继续保持非阻塞语义；
- 服务器旧命令可继续使用顶层兼容入口，新代码和 workflow 使用 canonical 模块路径。

### 验证

在 Windows 工作区使用 Python 3.13.7、pytest 9.1.1 和 Git Bash 实际执行：

- `python -m compileall -q scripts/local_ci scripts/dashboard/sync_gitee_results.py ...`：通过；
- prompt 与模块布局契约：`5 passed`；
- bridge unittest：`10 passed`；
- compile/pass/IR 性能回归：`15 passed`；
- dashboard contract/sync：`10 passed`；
- `scripts/local_ci` 和相关测试的全部 shell 文件通过 `bash -n`。

三组 Codex shell harness 仍需在 Linux 环境执行；Git Bash 调用原生 Windows Python 时，`/tmp` 路径、符号链接权限和默认 GBK 编码与测试预期的 Linux 语义不兼容。

---

## 2026-08-05：明确失败复现分类的中文表述

### 修改原因

“稳定失败”和“不稳定失败”可能被误解为 CI 系统本身的稳定性，不能直观表达“同一失败是否能够重复复现”。

### 修改内容

- 将面向人的 `stable_failure` 表述统一为“可稳定复现的失败”；
- 将面向人的 `flaky_failure` 表述统一为“非确定性失败”，表示相同用例在可比环境中的复跑结果不一致；
- 同步更新 success/failure prompt、报告 renderer、GitHub receiver 展示和开发指南。

### 兼容性与风险

- JSON schema 和 runner 中的 `stable_failure`、`flaky_failure` 枚举值保持不变；
- 不改变历史报告解析、状态协议或 Codex AI 非阻塞语义；
- 本次仅消除中文展示和诊断指令中的歧义。

### 验证

运行 prompt 模板契约、renderer harness 和 receiver/bridge 相关测试，确认枚举值保持不变且中文展示一致。

---

## 2026-08-05：区分已执行验证与后续验证建议

### 修改原因

`validation_strategy` 原表述为“实际采用或建议采用的验证策略”，容易把尚未执行的建议验证误解为已经执行并取得结果。

### 修改内容

- `validation_strategy` 只记录针对该文件实际执行的验证或诊断方式和结果；
- 未执行验证时必须说明原因，不得把建议描述为已执行；
- 尚未执行的后续验证建议统一写入 `suggested_tests`。

### 兼容性与风险

- 不修改 JSON 字段、schema 或 renderer；
- 仅收紧字段语义，避免 AI 报告混淆已执行证据和建议验证；
- success 与 failure prompt 保持一致，failure 模式额外明确“诊断方式”。

### 验证

运行 prompt 模板变量和关键输出契约测试，确认模板仍可由 runner 完整渲染。

---

## 2026-08-05：明确 finding 问题类型与严重度

### 修改原因

Prompt 已要求 finding 提供 `category` 和 `severity`，但此前没有明确两者的区别，也没有统一 HIGH、MEDIUM、LOW 的影响判断标准，容易按修复难度、代码行数或个人偏好分级。

### 修改内容

- 明确 `category` 表示根因对应的问题类型，`severity` 表示已确认影响程度；
- `HIGH` 用于关键路径错误结果、数据损坏、安全问题、普遍崩溃、公共 API 破坏等必须阻止合入的问题；
- `MEDIUM` 用于已确认但影响范围有限或存在明确规避方法的功能缺陷、回归、修正遗漏和边界/错误路径问题；
- `LOW` 只用于有可验证影响的低影响问题，不用于纯风格、命名偏好或普通未使用变量；
- 未使用变量只有在触发门禁、掩盖逻辑遗漏或造成其他可验证影响时，才按实际影响归类。

### 兼容性与风险

- 不修改 schema 中已有的 severity 和 category 枚举；
- 不改变 verdict 规则：HIGH 对应 FAIL，只有 MEDIUM/LOW 对应 WARNING；
- 继续禁止把代码风格、风险猜测和未来优化方向包装成 finding。

### 验证

运行 prompt 模板契约、renderer 校验和 GitHub receiver/bridge 相关测试，确认字段和枚举保持兼容。

---

## 2026-08-05：明确 runner 控制字段的信任边界

### 修改原因

Prompt 一方面要求使用 runner 注入的 `${DIFF_COMMAND}` 获取审查范围，另一方面又禁止执行不可信输入中出现的命令，可能让模型误判 `${DIFF_COMMAND}` 也不可执行。

### 修改内容

- 明确“可用输入”中的运行参数和控制字段属于受信输入；
- 明确 runner 直接注入的 `${DIFF_COMMAND}` 是允许执行的命令；
- 限定信任只适用于字段本身，字段指向、引用或承载的仓库、PR、日志、测试数据和 artifact 内容仍不可信；
- success 与 failure prompt 使用相同信任边界。

### 兼容性与风险

- 不新增或重命名模板变量，不修改 schema、runner 或 renderer；
- 消除控制命令与不可信内容规则之间的歧义；
- 不扩大对候选代码、PR metadata、日志或 artifact 内容的信任范围。

### 验证

运行 prompt 模板变量、关键输出契约和信任边界静态测试，确认两个模板仍可由 runner 完整渲染。

---

## 2026-08-05：对照 runner、schema 和展示链路审查 Prompt 一致性

### 修改原因

对 success/failure prompt 与 runner、schema、renderer、bridge、GitHub receiver 和容器 harness 进行交叉审查后，发现受信输入、安全边界、验证证据、测试状态和 fallback 报告存在可消除的语义漂移。

### 修改内容

- 将受信边界收窄到 runner 直接构造的 `${DIFF_COMMAND}` 和控制标量；路径字段仅用于定位，日志和 artifact 路径及内容始终不可信；
- runner 对日志提取的 artifact 路径执行规范化校验，只接受 `LOCAL_CI_ARTIFACT_ROOT` 的已有子目录；
- 不再把只读 `/workspace`、一次性 checkout 和无 Docker socket 描述为完整凭据隔离，明确候选容器快照、候选环境脚本、root、联网和 `danger-full-access` 的残余风险；
- `validation_strategy` 增加静态位置、artifact/阶段/SHA 和 `RUN-xxx` 命令引用要求；
- 明确 `stable_failure`、`flaky_failure`、`infrastructure_failure` 和 `insufficient_evidence` 的判定边界以及整体测试状态聚合规则；
- renderer 拒绝 passed/非零退出码、整体通过/命令失败等矛盾报告；测试或证据异常且没有产品 finding 时允许 WARNING；
- failure fallback 不再伪造产品 finding，改为 findings 为空、测试证据不足；
- bridge 对可稳定复现、非确定性、基础设施、测试生成和证据不足状态给出明确的非阻塞提示；receiver 保持现有非阻塞展示契约；
- 更新容器 harness 中已经失效的旧 prompt 文案断言，并增加伪造 artifact 路径和矛盾命令状态反例。

### 兼容性与风险

- 不修改报告根字段、schema 版本、状态枚举、结果路径或 Codex 非阻塞语义；
- `stable_failure`、`flaky_failure` 等持久化枚举保持不变，只收紧生成和校验规则；
- 当前 schema 仍没有独立的原始 Local CI 失败归因对象，失败阶段和归因主要写入 summary、merge recommendation 和 residual risks；若后续需要机器消费，应通过报告 v3 单独设计并迁移；
- runner 的命令数量和耗时仍主要依据模型结构化报告，当前属于约束告警，不是外部强制执行的资源沙箱。

### 验证

已运行 prompt/布局契约、renderer 状态矩阵、bridge unittest、性能/dashboard 回归、Python 编译、shell 语法和 `git diff --check`。Windows Git Bash 与原生 Windows Python 对 `/tmp`、symlink 和编码语义不同，完整 renderer shell harness 和 Codex fake Docker harness 仍以 Linux CI 结果为准。

---

## 2026-08-06：收敛 Local CI 稳定入口并移除兼容 wrapper

### 修改原因

服务器 systemd、cron 和仓库外脚本扫描确认，持久化配置只调用 `scripts/local_ci/poll_gitee_and_run.sh`。其余顶层 wrapper 已无运行时调用，继续保留会扩大路径契约并增加文档和 workflow 漂移风险。

### 修改内容

- 将 poller 完整实现固定在根目录 `poll_gitee_and_run.sh`，保持服务器 systemd `ExecStart` 不变；
- 删除 `orchestration/poll_gitee_tasks.sh`、其余 20 个顶层兼容 wrapper 和 `_compat_entrypoint.py`；
- workflow 除稳定 poller 入口外全部引用分层目录中的 canonical 路径；
- 布局测试要求根目录只能存在 `poll_gitee_and_run.sh`，并禁止恢复旧 poller 模块；
- runner 快照前检查完整运行时文件集，并从快照中移除 `__pycache__`、`.pyc` 和 `.pyo`；
- 同步 README、DEVELOPMENT_GUIDE 和 CI 指南中的入口、调用示例和职责说明。

### 兼容性与风险

- 不修改 task ref、结果目录、artifact 名称、报告 schema、status context 或性能基线协议；
- 两份服务器 systemd unit 继续使用原 `poll_gitee_and_run.sh` 路径，不需要修改或重启才能适配本次源码布局；
- 历史 workspace、Codex checkout 和 runner 快照中的旧脚本副本不是持久化调用方，不影响删除；
- 新代码不得重新增加根目录 wrapper。

### 验证

运行 Local CI 布局和 Codex 契约测试、bridge unittest、性能/dashboard 回归、Python 编译、全部相关 shell 语法、工作树旧路径扫描和 `git diff --check`。

---

## 2026-08-06：将 Local CI 专属测试归入模块目录

### 修改原因

Local CI bridge 和 Codex harness 原先位于仓库外层 `tests/`，与产品 smoke 测试混在一起，导致测试职责、workflow 收集范围和安全控制面边界不清晰。

### 修改内容

- 保留产品级 `tests/test_smoke.py` 在外层 `tests/`；
- 将 Codex renderer、容器和 setup harness 移入 `scripts/local_ci/codex_ai/tests/`；
- 将 bridge 单测移入 `scripts/local_ci/results/tests/`；
- 保留 Local CI 布局测试在 `scripts/local_ci/tests/`；
- 更新 workflow、Ruff 范围、README、DEVELOPMENT_GUIDE 和测试命令；
- workflow 显式执行 Python 契约测试和三个 Shell harness，避免依赖 pytest 自动收集 `.sh`。

### 兼容性与风险

- 不改变测试逻辑、测试数据协议或产品 smoke 测试归属；
- 移入 `scripts/local_ci` 后，测试文件属于受保护的可信控制面，修改会遵守 Local CI 安全扫描规则；
- runner 快照会包含这些测试，但不会自动执行它们；
- 旧外层 Local CI 测试路径不再作为稳定命令。

### 验证

运行 `scripts/local_ci/codex_ai/tests`、`scripts/local_ci/tests`、`scripts/local_ci/results/tests`、bridge unittest、Shell harness、Python 语法和 `git diff --check`。
