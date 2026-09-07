# 验收覆盖与边界

汇总见 [verification.md](verification.md)，原始命令、退出码、测试数量和源码摘要见 [verification.json](verification.json)。8 套测试共 **421 项通过**。对应提交在 [工作记录](../ci_refactor_log.md) 与 `commits.json` 中登记。

| 顶层要求 | 实现与验证证据 |
| --- | --- |
| 顺序门禁、类型字段、外部 fork 审批 | `gateway_v4.py`；`scripts/ci/tests/test_gateway_contract.py` 测试真实模板解析、审批保护缺失/有效、前置状态、生命周期和 gateway DAG；API 测试保留。 |
| 冻结快照、去重、取消、拒绝旧结果 | 不可变任务及 current/cancel；本地 bare Git 实际投递与并发写入、重复事件、身份变动、取消后 ACK 拒收。 |
| Codex 主导、可信最低集合、交错审查 | `policy.py`、`supervisor.py`、MCP；调用方选择执行顺序，依赖检查和最终必检门禁真实执行；生成复现由实际 Python 子进程运行，验证两次候选失败与 base 通过。 |
| 十个独立工具、前端/后端能力差异 | tools 26 项测试验证命令调用、wheel 摘要与安装来源、空集合/无效性能数据、真实文档/工作流契约检查；昂贵构建和硬件命令边界替换。 |
| 架构证据与 AI 高风险归因 | 可信规则原文、变更代码引文、相同脚本/环境证据；不存在的契约、缺 base 或不足复现不能形成高风险阻断。 |
| 隔离与取消 | Executor 8 项测试使用真实非 root UID、独立 venv、实际子进程；覆盖忽略 TERM/脱离 session 子进程清理、0077 umask、环境变量和参数限制。 |
| 模型中断、OOM、重启和发布恢复 | Agent CI 35 项测试覆盖有限 API 恢复、OOM 降并行度、已成功项复用、依赖重建失效、setup/磁盘异常、封存结果只重发、会话身份、缺失/部分回执；Codex 进程/API 使用 mock。 |
| 环境轮换、LLVM、回退 | Environment 25 项测试运行实际 registry/lease、临时 Git 镜像、归档摘要与缓存校验；覆盖占用保护、候选失败保留活动代际、后端失败不降级、取消等待锁与真实子进程、控制 SHA 变化。Docker daemon 使用替代边界。 |
| 性能测量与报告 | 工具和前端性能回归验证无数据、无 profiling event、无效 roundtrip、基线不匹配等失败/不可比较情况；纯测量变化不升级为构建阻断。没有执行真实硬件性能测量。 |
| 完成闭环 | `test_worker_through_actual_receiver_and_git_receipt`：真实 Worker/SQLite/GitRelay → GitStore/collect_results → 模拟 status/comment/Pages → acknowledge → 真实 receipt → complete。 |
| 监控与通知 | 部署/监控 23 项测试验证独立心跳、主机/中转区分、异常去重、恢复通知、待发通知保存、安装回滚和迁移顺序；SMTP 使用 outbox/mock。 |
| 历史兼容及回归 | 235 项历史解析/本地契约、45 项前端/性能/Dashboard 回归、9 项 API 契约通过；历史结果不能满足 v4 任务。 |

另外通过与 Basic CI 相同版本 Ruff 0.15.22 的阻断规则、新增模块 `F,E9`、Python/shell 语法及两个分支 `git diff --check`。部署配置模板的只读预检按预期失败，见 [preflight-example.json](preflight-example.json)：空公司镜像、账号、依赖和通知配置没有被当作可上线环境。

本次没有实际推送 GitHub/Gitee、部署或启动公司服务器服务、创建 Docker 环境、调用公司模型、编译 LLVM/厂商后端、运行芯片/仿真器或发送邮件；没有执行被禁止仓库/分支的远端操作。所有通过结论均限定为本机代码与模拟边界验收。实际环境、网络权限、模型响应、厂商组件、硬件、Pages 和邮件送达需在上线阶段另行验证。
