# Triton Anchor CI v4

实施依据为用户提供的 `new_CI.md`、流程图及已确认的全面重构计划；Skill 结构按 2026-09-08 的 CI Skill + Harness 调整要求直接落地。当前入口说明见 [Local CI](../scripts/local_ci/README.md)，上线材料见 [部署与回滚](../scripts/local_ci/deploy/README.md)，过程见 [工作记录](ci_refactor_log.md)。此前 CI 说明保留为历史设计，不能用于部署 v4。

## 从事件到结果

`main` 只把事件、人工请求与定时任务路由到 `CI_dev`。可信网关依次完成 PR 字段检查、Basic CI、API 兼容性、Security Gate，生成统一 review card；仅外部 fork 进入人工审批。失败会反馈原因，不发布 Local CI 任务。不配置分支保护或破坏性变更豁免。

网关冻结 merge/base/head、CI 实现 SHA、PR 元数据与 LLVM SHA。代码 refs 写入后才发布不可变 task manifest 和 current 指针；重复事件保持幂等。PR 提交、状态、目标、描述和标签变化使旧任务失效，Gitee 取消记录供 Poller 消费。审批后和回写前再次核对身份。

Poller 校验快照及已安装控制代码，领取对应版本代际。Harness 显式加载 [local-ci/SKILL.md](../scripts/local_ci/skills/local-ci/SKILL.md)，再按入口声明读取四份 references，启动公司配置的单一 Codex 会话。Skill 的主流程规定工作循环，`agent_ci/policy.py` 通过真实 diff 决定不可删减的最低检查。模型可以增补工具、安排顺序、在构建期间开展只读审查。

Codex 的业务工具入口绑定当前任务 MCP；可信 worker 控制容器、日志、状态与发布。模型认证保留在专用 Codex 账户，候选代码由另一执行账户运行。十个工具仍位于 `tools/`，经 `start_check(tool_id)` 独立执行，wheel 构建与安装拆开；候选/base 的安装及缓存隔离。生成测试保留源码、命令、退出状态及归因证据。结构化结果来自执行器，模型报告不能代替通过记录。

Codex 的业务决定只有继续或阻塞。`pass/fail/infra_error` 说明证据结果或失败原因，`publishing/complete` 说明本地上传进度。缺少必要证据或恢复预算耗尽时不能生成通过结果；本地 complete 也不等于通过。运行期禁止多 Agent；GitHub 前置流程继续严格 Basic → API → Security 串行。

`finish` 封存 result 和证据，Codex 工作结束。上传阶段不持有构建锁；失败只重发同一封存结果，不恢复模型。成功上传 Gitee 后 worker 将本地任务置为 complete。GitHub 接收器独立校验任务和快照 diff 的最低检查，发布 status、统一 comment 和 Pages，不向 Gitee 写回执。

按用户确认保留 status → comment → Pages 顺序；GitHub Actions 显示发布失败并由后续定时任务重试。PR 的两个 Commit Status context 保持现有名称，它们是检查结果的权威状态；整次 GitHub 发布是否完成还需查看对应 Actions 运行。没有跨系统完成确认，PR status 成功可早于 Dashboard 更新。

## 协议与恢复

| 位置 | 内容 |
| --- | --- |
| Gitee `local-ci-control/tasks/<task_id>.json` | 不可变 v4 任务；身份包含 worker SHA、元数据摘要及 full 请求 |
| `current/<subject_digest>.json` | 当前 PR 或分支有效任务 |
| `cancel/<task_id>.json` | 取消原因与可选替代任务 |
| Gitee `local-ci-results/runs/v4/<task>/<run>/` | 封存结果、基础检查及生成测试证据 |
| `local-ci-results/retention/v4/<task>/<run>.json` | 过期结果的身份、摘要与时间；防止误用更旧结果，与发布确认无关 |
| 宿主机 `state_dir` | SQLite journal、execution、outbox、环境 registry、lease 与恢复上下文 |

声明式 schema 位于 `agent_ci/schemas/`；运行时还检查摘要、ref 安全、仓库白名单、merge parents、精确 LLVM、依赖执行 ID 和当前任务，不能仅以 JSON schema 合法判定任务可信。

重启从 journal 恢复；成功项仅在环境及依赖证据仍一致时复用。OOM 自动降低并行度重试一次，API/网络有限重试。未完成测试结果仍显示基础设施异常，即使已上传并本地 complete 也可显式续跑。相同任务重新投递不会强制重建；`worker.py --resume TASK_ID` 对已上传 infra_error 创建新 run，对待上传任务继续原 outbox。

## 环境与运维

Triton 3.0 必须保留后端、FlagGems 和性能能力，其他版本只提供前端。缺少已声明能力属于环境异常。每日错峰创建常驻候选环境，验证成功后只切换新任务；已有 lease 和上一可用代际受保护。新 LLVM 使用可信镜像/源码配方与精确摘要，3.0 后端失败不会降级。

常驻容器复用系统、LLVM 和依赖底座，任务目录由 `agent_ci/workspaces.py` 管理。任务前持久登记并标记 dirty，任务后确认专用 UID 进程退出，验证共享文件指纹、公共目录及真实设备状态后才封存。失败代际隔离，停止未确认时禁止新构建。成功任务封存后立即回收工作目录；失败/待恢复目录默认保留 24 小时，受 100 GiB 逻辑预算约束。保留日志和 outbox，回收后使原安装状态的通过记录失效，续跑重建；同配方不同代际也不能直接复用安装状态。

重启先在原代际处理残留进程和租约，兼容登记以前完成任务遗留的目录。显式 `--resume` 与 worker 共用独占锁，需先停止服务再操作。目录、代际与磁盘状态进入独立健康报告和现有告警机制。此轮不改变 GitHub 事件、门禁或 status → comment → Pages 的顺序。

独立 health timer 发布主机、服务、环境、Codex、磁盘、执行及发布状态；GitHub watchdog 读取心跳并通过 SMTP 发异常/恢复通知。中转不可达与主机离线分别报告。告警状态和待发通知跨定时运行保存。

配置模板故意留空实际公司镜像、模型目录、后端与 SMTP 来源，预检要求补齐。部署必须使用与投递 worker SHA 一致的干净控制 checkout；切换代码前停止旧接单并处理在途任务。旧凭据与公司模型不自动改写。

GitHub 侧需配置 `GITEE_RESULTS_REPO_URL`、`GITEE_USERNAME`、`LOCAL_CI_HEALTH_URL`、`LOCAL_CI_WORKER_ID` 变量和 `GITEE_TOKEN` secret；SMTP 使用 workflow 中列出的 `LOCAL_CI_SMTP_*` secrets/变量。`local-ci-fork-approval` environment 必须配置实际 required reviewers，缺配置不得放行外部 fork；Pages 发布使用 `github-pages` environment 和 Actions 发布源。部署材料不替管理员创建审批者、填入凭据或调整分支保护。

私有心跳仓库在 GitHub 单独配置只读 `GITEE_HEALTH_TOKEN`；worker 的发布凭据仍使用服务器私有配置。未配置中转或心跳地址时明确失败，不自动拼出公司的实际地址。

审批检查只读取 environment 的保护规则，相关 job 配置 `actions: read`，符合 [GitHub Get an environment 接口](https://docs.github.com/en/rest/deployments/environments#get-an-environment) 的权限要求。

## 验收范围

本机运行真实网关、Git 中转、调度、MCP、状态机、工具接口、结果接收及保留周期代码，Docker、模型、硬件、GitHub HTTP、SMTP 等外部边界替换为 fixtures。用真实 Python 子进程验证归因与终止行为，用本地 bare Git 验证任务投递、结果上传和过期清理。

运行 `python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/ci-v4-verification` 可重现验收。最终报告单独记录通过项及源码摘要。模拟结果不代表公司模型、LLVM/后端真实编译、硬件、实际邮件或线上 GitHub/Gitee 已通过验收。

任务回收与环境复用检查见 [最新验收](ci_cleanup_verification/verification.md) 和 [覆盖说明](ci_cleanup_verification/coverage.md)。单向交付的 472 项报告保留在 ci_oneway_verification；Skill 结构迁移的 442 项报告保留在 ci_skill_verification，原回执相关测试仅代表当时实现。
