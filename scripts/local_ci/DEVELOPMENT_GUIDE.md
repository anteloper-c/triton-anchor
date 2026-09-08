# Local CI 开发指南

本文描述当前 v4 实现。工作流程见 [README.md](README.md)，主机配置见 [config.example.json](config.example.json)，工具参数见 [tools/README.md](tools/README.md)。仓库之外的设计原文、用户提示词、操作日志和认证材料不属于发布内容。

## 受信边界

1. GitHub 冻结 repository、head/base/tested SHA、worker revision、PR 信息、前置检查和审批，形成 `triton-anchor-local-ci-task-metadata/v2`。外部 PR 的审批必须绑定精确提交身份。
2. `runtime/poller.py` 依据主机 `branch_profiles` 选择环境。`runtime/control.py` 校验实际控制文件，`runtime/relay.py` 检出精确提交并复核取消和身份。PR 内容不能指定 Docker 配方、宿主命令或替换控制程序。
3. `runtime/engine.py` 持有 worker 租约、任务目录、Codex 生命周期和取消监控。受信 launcher 使用 root 所有的解释器；构建 UID 1000 与 agent UID 1001 分离，测试 venv 是 seed 的任务副本。
4. `runtime/broker.py` 执行登记工具的计划，记录真实命令、退出码、终止原因及产物哈希。agent 只能提交请求和审查，不能写主机 receipt。后续工具重新核验依赖产物。
5. `runtime/report.py` 将必检、执行事实和 AI 审查合并为 v4 结果。缺少必检、无效证据、源码被改写或任务失效均不能成功；GitHub 再校验结果与当前任务才更新状态。

生产控制面身份覆盖 `scripts/local_ci/`、`scripts/dashboard/` 和 `.github/`。实际文件必须与冻结 worker SHA 完全一致；部署副本需要完整可信 manifest。`local_acceptance=true` 是明确的开发标识，不能用于生产成功回写。

## 增加或修改工具

在 `tools/basic_tools/runner.py` 登记工具，并在 `tools/` 下实现实际行为，不再引入独立的旧式脚本编排目录。入口为：

```python
plan(tool_id: str, context: dict, parameters: dict) -> dict
```

返回值含 `tool_id/status/reason/dependencies/commands/artifacts`。每条 command 使用 argv 数组、cwd、env、timeout，不能把 agent 文本拼接为 shell。`plan()` 在主机运行，不假定容器绝对路径也存在于主机；路径、checkout 和依赖检查放到容器执行。

`context` 由 broker 构造，包含身份、容器 source/artifact/tools 目录、任务 Python、完整受信 profile 和已完成检查。agent 参数只表达有界任务选项，例如 build jobs、FlagGems 算子集合或性能 repeat；不得接受 profile、宿主目录、命令字符串、成功状态或 baseline 身份。新增参数必须有 allowlist、范围检查和清晰错误。

每次运行必须关联实际被测 SHA。产物记录 SHA-256，安装及后续检查再次确认没有被替换。重试更新当前检查，使依赖检查失效并保留命令账本，不能复用同名旧 wheel 或旧日志冒充执行。依赖关系只表达必要产物，不应把 Codex 调度退化为固定步骤列表。

后端、FlagGems 和性能只适用于 3.0；适用环境的配置缺失应失败。FlagGems `full` 必须由受信手动触发身份解锁。性能需要候选实测，可信 baseline 须匹配基准 SHA、profile、LLVM 和内容哈希；没有基线时保留未比较状态。人工 `runner.py --execute` 标记 `trusted_receipt=false`，不替代 broker 门禁证据。

## AI 审查与任务内辅助脚本

`ai_ci_program.md` 编排意图理解、影响判断、工具调度、失败定位与最终审查。`tools/ai_review_tools/` 规定架构和专项审查维度；schema 定义输出结构。修改编排、契约或基础工具属于长期规则修改，需要维护者采纳。

架构审查须引用实际源码文件和有效行号，说明与变更有关的判断。文件存在、空泛结论或自报 JSON 不构成测试成功。agent 可以将定向 Python 用例放入任务 `artifacts/custom/`，通过 broker 的 `custom_test` 执行并关联 receipt。辅助脚本只服务当前任务，不得修改被测源码、受信控制程序、共享 seed 或发布凭据。

## 恢复协议

`execution.json` 阶段为 `preparing/running/publish_pending/published`。Poller 启动后先恢复未完成执行与待发布结果，再接新任务。全局锁防止双 Poller 同时恢复；worker 租约防止维护或其他任务并行占用容器。

执行恢复使用原 run_id、源码清单、已有检查、主机产物指纹和 agent 计划。`active-command.json` 对应的中断命令不能视为完成；重试重新验证依赖证据。清理需覆盖脱离父进程组的后台进程，失败保留租约，不能通过删除租约强行继续。

发布失败保留原 result 与 artifacts。发布器逐文件核验已发布 manifest 的不可变性并拒绝日志哈希失配；同一 run 重试不得改写已发布产物。发布诊断可恢复原 Codex session，但不给构建工具，凭据及实际推送仍由主机持有。损坏记录不能阻断其他待发布结果；健康状态要区分执行、待发布与完成。

每日重建由 `maintenance/` 管理受信配方、UTC 窗口、排空、全局构建锁、同名替换与回滚。健康发布使用独立 checkout 和锁。SMTP、环境和 watchdog 的本机配置不应提交到示例文件。

## 验证方法

从仓库根目录，在独立测试环境中执行：

```sh
python3 -m pytest -q scripts/local_ci/tests scripts/ci/tests
python3 -m pytest -q python/triton_anchor/tests/test_dashboard_sync.py python/triton_anchor/tests/test_dashboard_contract.py
```

| 测试 | 主要验证 |
| --- | --- |
| `test_tools*.py` | 工具执行边界、wheel 身份、性能解析和比较 |
| `test_runtime_protocol.py` | 必检、架构证据、身份、取消、实际 bare Git 发布和不可变重试 |
| `test_engine_recovery.py` | 准备失败可发布、记录损坏隔离、健康恢复、发布失败不重复执行 |
| `test_environment_selection.py` | 冻结 Git blob/gitlink 的 LLVM 选择、受信配方与任务目录隔离 |
| `test_performance_baselines.py` | 已发布基线的身份、成功 receipt、真实文件哈希及显式配置优先级 |
| `test_review_control.py` | 控制文件身份与审查可信边界 |
| `test_github_result.py` | GitHub 回写前的完整任务与结果校验 |
| `test_maintenance_*.py` | 固定 worker、排空/回滚/恢复、健康和进程清理 |
| `test_dashboard_agent.py` | v4 agent 报告展示与兼容 |

`CI_TOOLS_TEST_PYTHON=/absolute/path/to/isolated/python` 开启真实纯 Python wheel 的构建、安装和 import 测试。解释器需安装 build、setuptools、wheel；测试会安装测试 wheel，必须选择可写的专用环境。进程清理验收只能在独占容器内以 root 设置 `LOCAL_CI_PROCESS_CLEANUP_TEST=1`，不能在通用宿主上开启。

旧性能解析测试已迁到 Local CI 测试目录。`python/triton_anchor/tests` 的编译器行为测试仍需正常编译器环境，不能因缺少本机后端而删除或伪造通过。命令适配器/本地 bare Git 测试、真实轻量容器链路及合成 fixture 构建，均不等于实际 Triton/LLVM/后端硬件验收，应分别记录。

变更 workflow 后用 actionlint 校验 worker 和 main router。合约、字段或状态变化时，同步 schemas、GitHub 校验、Dashboard、配置和文档；不能只更新提示词。交付前检查本机配置、设计原文、操作日志及认证材料未进入 Git diff。
