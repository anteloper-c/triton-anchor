# Local CI 合并记录

2026-09-10。代码整合于独立工作区 `CI-restart/local-ci-merged`，分支 `local-ci-unified`。原始 checkout 保留。本次将统一代码发布到 `likehupochuan/triton-anchor` 的 `local-ci-unified` 分支；未执行服务部署或修改平台门禁配置。

## 来源与基线

按用户补充要求核对两个 GitHub 公开分支的最新提交，均与 `new_ci.zip` 修订指南一致：

| 来源 | 采用的提交 | 用法 |
| --- | --- | --- |
| [anteloper-c / ci_repo](https://github.com/anteloper-c/triton-anchor/tree/37d3a5c9cea35f82ba5d7ffe13a8e8525df1eb62) | `37d3a5c9cea35f82ba5d7ffe13a8e8525df1eb62` | 固定 SHA 源码归档，核对 tools、策略、反馈与 Dashboard |
| [likehupochuan / CI_dev](https://github.com/likehupochuan/triton-anchor/tree/ce440df672e9d3dbd230f651e36b91ddef67b510) | `ce440df672e9d3dbd230f651e36b91ddef67b510` | 从完整本地 checkout 创建新分支，保留 Gateway 与环境骨架 |

按模块整合，编译器产品代码沿用 CI_dev 基线。变更集中于 CI、部署、展示和相关文档。

依据为包中 2026-09-10 修订的 `new_CI.md`、`Local_CI_工作合并指南.md` 和 `AI_CI_PROGRAM.md`。文档中“尚未合并或部署”描述其编写时状态；旧交接中的四 UID、Skill、SQLite 和运维通知不作为本版要求。共享聊天链接未返回可读取正文，因此上下文以用户提供的压缩包为准。开发取源码使用 GitHub；运行链路仍为 **GitHub → Gitee → Local CI → Gitee → GitHub**。

输入 SHA-256：

- `new_ci.zip`：`4972f31fcfe6419b5b2b519defc9db308c01612008ac2a155be59cc53c4f6ab5`。
- ci_repo 固定 SHA 归档：`1f1bcf419a4e1bcb0c79e5293df96c8a27fae13affe9bf372913f1c3e4521209`。

## 模块分析与取舍

| 模块 | 差异及最终选择 | 实现 |
| --- | --- | --- |
| Gateway | A 反馈完整但接收长期等待；B Python 骨架有冻结与短接收。保留 B，补齐审批证据、任务专属源码/子模块 refs、manifest 最后发布及固定控制版本。 | [gateway_v4.py](../scripts/ci/gateway_v4.py)、[ci-receiver.yml](../.github/workflows/ci-receiver.yml) |
| tools | A build/install/tests/smoke 独立，参数、依赖与计划集中；B 有环境和测量修正。以 A runner/actions 为唯一实现，将 B 修正归入对应模块，删除旧 run_tool/deterministic 入口。 | [runner.py](../scripts/local_ci/tools/basic_tools/runner.py)、[actions.py](../scripts/local_ci/tools/basic_tools/actions.py) |
| Agent 与执行 | 复用 B MCP/supervisor 传输与生命周期，改为直接 program、一个非 root 用户、统一记录及证据解析。MCP 不再复制工具/参数注册表。 | [program.py](../scripts/local_ci/agent_ci/program.py)、[executor.py](../scripts/local_ci/agent_ci/executor.py)、[supervisor.py](../scripts/local_ci/agent_ci/supervisor.py) |
| 环境 | A 常驻容器和部分共享后端状态不符合任务隔离；保留 B Rootless 任务容器、精确 LLVM/依赖与控制快照，迁入 ops_maint。 | [manager.py](../scripts/local_ci/ops_maint/manager.py)、[runtime.py](../scripts/local_ci/ops_maint/runtime.py) |
| 状态与恢复 | 采用宿主文件状态，移除 SQLite/outbox。未封存任务重启新 run，已封存只清理与补传，不接管半安装环境。 | [state.py](../scripts/local_ci/agent_ci/state.py)、[workspaces.py](../scripts/local_ci/agent_ci/workspaces.py) |
| 证据与交付 | 统一工具 ID、执行摘要、报告和工件关联。大证据走 Release 附件，小 JSON 走 Git，执行结论与传输状态分开。 | [delivery.py](../scripts/local_ci/agent_ci/delivery.py)、[protocol.py](../scripts/local_ci/agent_ci/protocol.py) |
| 展示与运维 | 保留 A 的任务与证据、全量算子、后端与性能三视图；健康独立发布。取消邮箱、运维 Issue 和 GitHub 侧 watchdog。 | [Dashboard](../dashboard/README.md)、[ops_maint](../scripts/local_ci/ops_maint/README.md) |

受信 policy 在派发、Worker 和接收端计算最低范围；混合改动取并集，纯测试改动执行对应测试，AST 等价不能自动免测。full 在支持的环境包含全量 FlagGems。后端构建默认只依赖环境；确需候选前端的配方可显式追加依赖。

内置命令与原生命令共用任务 checkout、venv 和环境配方。`record_check` 关联实际观察到的执行及其运行期间产生的报告；旧报告、结束后写入或被改写的报告、零用例、全部跳过、被 shell 掩盖的失败不能获得通过。测试进程记录实际模块导入路径和摘要，避免测试源码却声称验证 wheel。修改产品源码的结果归属修改后对象，新增针对原始 wheel 的复现可以计入证据。同一命令可关联多个真实覆盖的检查，原始执行和日志不重复打包。

单命令取消按进程组，任务结束才停整个容器。Agent 中断时未完成命令落为未完成记录。宿主 `runs/<task>/<run>` 保留状态、日志和证据，`work/<task>/<run>` 可清理。状态原子替换并同步落盘，封存后结果本体不变。停止或丢失容器仍清理对应临时目录；无法确认清理成功时保留待清理状态。

文本日志/报告封存导出时脱敏，原始记录保持宿主私有。必要附件 pending 时 GitHub summary 保持 pending；可选附件独立重试并到期。接收端重查 task/run、被测 SHA、工具、scope 和执行关联，旧任务不能覆盖新 PR 结论。稳定门禁为 `local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`。

health 采集和 watchdog 在同一 CI 主机独立运行，上传前裁剪公开字段。Gitee 不可读为 unknown；公开快照使用无父提交和旧 SHA 条件更新。整机停机时 watchdog 同时停止，页面通过过期时间反映状态。

## 效率与简洁性

复用有效环境和检查、分开构建与安装、支持定向选测与原生证据抵扣，避免无变化环境每日强制重建。性能沿用 A 参数与比较接口，吸收 B 的 IR 内容、真实 pass timing、采样和 FlagGems 工作目录修正。baseline 必须匹配版本、环境、LLVM 和测量参数；不可比较时明确标记，性能退化默认提示。

首版一个 Worker 同时执行一个重构建任务，不增加分布式队列、数据库、第二 broker 或多 Agent 运行框架。以上属于代码层面的重复工作削减，未在目标硬件测量加速比例。

## 验证记录

最终完整回归 **259 项通过，0 失败、0 错误、0 跳过**（约 219 秒）：

```bash
python3 -m pytest scripts/ci/tests scripts/local_ci/agent_ci/tests \
  scripts/local_ci/tools/tests scripts/local_ci/ops_maint/tests -q \
  --junitxml=../merge-verification/pytest.xml
```

Dashboard 的 `node --test dashboard/tests/v4-data.test.cjs` **4 项通过**。Ruff F/E9、Python 格式检查、77 个 Python 文件语法、Bash 脚本、6 份工作流 YAML、6 份 schema/feed JSON、Dashboard JavaScript 语法及当前文档链接检查通过，`git diff --check` 无错误。浏览器检查覆盖三个业务视图和数据展示，临时测试数据及预览服务已清理。

本机记录保存在相邻 `merge-verification/`：`pytest.log`、`pytest.xml`、`dashboard-tests.log`、`static-checks.json`、`validation.json`、`sources.json` 和最终 `local-ci-unified.patch`。代码按模块整合为一个提交，发布分支为 `local-ci-unified`。提交与推送阶段仅更新本记录的发布状态，运行代码保持上述验证版本。

回归覆盖本地 bare Git 投递与过期任务、必要附件及重传、真实子进程取消、文件状态重启、native/builtin 同等证据、安装来源变化、rootless 生命周期命令构造、健康与保留行为。Worker 集成测试使用真实临时 Git 和文件系统，Docker、模型及附件 HTTP 服务使用测试替身。

## 部署验收

按 [Gateway 配置](../scripts/ci/README.md) 和 [运维说明](../scripts/local_ci/ops_maint/README.md) 完成：

1. 填写实际 Gitee 源/结果和子模块镜像、health 仓库、模型凭据、Rootless 资源、设备/仿真权限与精确 profile；运行配置预检、环境准备、真实工具验证和 runtime probe。
2. 在目标仓库验收附件上传、响应丢失后的去重、下载校验、删除与配额；跑完整 PR 闭环，验证断网恢复、取消和 Worker 重启。
3. 将审查后的代码提交并发布为固定受保护 ref/SHA。暂停旧版派发，排空或取消在途任务，同步 Gateway/Worker 控制版本后恢复；旧封存包保留对应补传能力。
4. 查询确认 GitHub required checks、外部 fork 审批和 Pages 实际生效，部署独立 health 页面。

旧目录入口已移除，旧配置和路径不能原样替换。源码配置不表示平台已完成设置；历史文档中的生产验收数字不适用于本统一版本。
