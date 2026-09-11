# Local CI 统一版本

本目录整合 `ci_repo` 的分阶段 tools 与业务展示、`CI_dev` 的任务冻结与环境准备。唯一业务链是 **GitHub → Gitee → Local CI → Gitee → GitHub**。

## 运行模型

Gateway 顺序执行 Basic/API/Security、按需审批，冻结 PR 合并结果与 base/head，并在源码及需要随任务检出的子模块对象到达 Gitee 后发布 task manifest。Worker 使用相同固定控制版本，按受信 diff policy 计算最低范围。纯文档走轻量 control_plane；仅改测试必须执行对应测试；AST 等价不自动免测；full 在支持的环境包含全量 FlagGems。

根目录 `FlagGems` 使用服务器 profile 预置依赖，不参与网关子模块镜像与任务检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍经 Gitee 固定到对应 Git 对象。旧 `CI_dev` 分支式 refs 的活动记录会被识别并跳过，不执行、不回写通过，也不阻断新版任务；旧记录不列入新版看板当前任务列表，历史文件无需清理。

Worker 从 [AI_CI_PROGRAM.md](AI_CI_PROGRAM.md) 直接启动一个 Agent，每任务一个 Rootless 容器、一个非 root 执行用户。Agent、构建、安装和测试使用同一任务环境。base 与 experiment 按需准备，表示数据版本。生产配置、状态文件、命令日志与封存包由宿主管理。

[tools](tools/README.md) 的 `runner.plan(tool_id, context, parameters)` 是工具目录、参数与依赖的唯一来源。MCP 仅提供任务接口。前后端 build/install/tests/smoke 分开；后端 build 默认只依赖 environment，安装才依赖前端安装。原生命令可通过 `record_check` 关联实际报告，使用与内置工具相同的检查判据；既有有效执行可以复用。零用例、全部跳过、缺少报告、被 shell 掩盖的失败均不能变绿。相关测试还检查实际测试进程的 wheel import 来源。

## 状态、证据与发布

```text
state_dir/
  runs/<task>/<run>/
    task.json, state.json, commands.jsonl
    logs/, artifacts/, sealed/
  work/<task>/<run>/
```

`state.json` 串行、原子更新；`commands.jsonl` 仅接纳完整完成记录。阶段为 `preparing/running/sealing/publish_pending/published`，与 `pass/fail/infra_error/cancelled` 结论分开。没有 SQLite 或独立 outbox 状态机。

Agent 会话中断且 Worker/容器仍完整时可复用该 run 的有效记录。Worker 重启时，未封存任务停止旧容器、保留证据并创建新 run；不接管半安装环境。已封存任务只补清理/上传，网络故障不重编。单命令取消只针对该进程组，任务结束才停止整个容器。

Git 保存小 result、execution-summary 与 delivery-index；压缩日志、JUnit、复现与必要测量走 Gitee CI Release 附件。摘要验证通过但必要附件未交付时，GitHub summary 仍 pending。可选大工件不阻塞完整结论。默认保留 30 天，必要 pending 受保护；容量不足暂停新任务。

Dashboard 保留任务与证据、全量算子、后端与性能三个业务模块。[ops_maint](ops_maint/README.md) 统一环境、配置、部署、健康与清理。公开 health 的采集及 watchdog 均在同一 CI 主机独立运行；上传前裁剪私有字段，不保留 SMTP/运维 Issue 通知。整机停机时，页面根据最后快照显示过期，同机 watchdog 无法继续主动观察。

## 开发与部署

本地回归：

```bash
python3 -m pytest scripts/ci/tests scripts/local_ci/agent_ci/tests \
  scripts/local_ci/tools/tests scripts/local_ci/ops_maint/tests -q --import-mode=importlib
```

运行测试需 Python 3.10+、pytest、PyYAML、Git；编译测试还需要相应 LLVM、Python 构建依赖、后端及设备/仿真环境。控制程序测试不代替真实工具链验收。

部署先填写 [配置模板](ops_maint/config.example.json)，完成 Gitee 源/子模块镜像、模型凭据、精确镜像与 profile 实测，再按 [部署说明](ops_maint/README.md) 安装。Gateway 自动解析 local-ci-unified 的控制 SHA，所有 PR 目标分支均可派发；接收工作流和 required checks 配置见 [GitHub 侧配置](../ci/README.md)。实际切换前暂停旧版接单并排空在途任务。

开发时以 `runner.py` 作为工具、参数和依赖的唯一入口，`state.py` 作为文件进度的唯一写入入口；CLI 与 MCP 共用工具计划，正式结论共用 `evidence.py` 的报告判据。修改选测、取消、重启、发布或证据行为时验证相应边界，避免只断言实现细节。

原生命令的 pytest 检查使用 `tools/basic_tools/pytest_exec.py --installation <installation.json> --import-report <产物目录>/import-origin.json -- <pytest 参数>`，同时输出 JUnit，再通过 `record_check` 关联实际执行；测试进程的 import 记录用于确认验证的是已安装 wheel。
