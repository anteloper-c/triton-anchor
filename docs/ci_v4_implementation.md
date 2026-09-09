# Triton Anchor CI v4

实施依据为用户提供的 `new_CI.md`、流程图及已确认调整。当前架构为普通 CI 用户运行 Rootless Harness，每个任务 attempt 使用独立容器，容器内包含 Codex 与构建测试身份。入口见 [Local CI](../scripts/local_ci/README.md)，上线材料见 [部署与回滚](../scripts/local_ci/deploy/README.md)，过程见 [工作记录](ci_refactor_log.md)。以前的常驻环境设计及验收仅作历史参考。

## 从事件到结果

`main` 保留事件、人工请求与定时任务的必要路由；`CI_dev` 提供可信实现。GitHub 网关依次完成 PR 字段检查、Basic CI、API 兼容性、Security Gate，再生成统一 review card；仅外部 fork 进入人工审批。失败会反馈原因，不发布 Local CI 任务。本次不配置分支保护或破坏性变更豁免；既有 Basic → API → Security 串行依赖保持不变。

网关冻结 merge/base/head、CI 实现 SHA、PR 元数据与 LLVM SHA。代码 refs 写入后才发布不可变 task manifest 和 current 指针，重复事件保持幂等。PR 提交、状态、目标、描述或标签变化使旧任务失效，Gitee 取消记录供 Poller 消费；审批后和回写前再次核对身份。

Poller 校验任务及已安装控制版本，从已验证镜像创建当前 task/run 的独立 attempt。宿主机 Harness 显式加载 [local-ci/SKILL.md](../scripts/local_ci/skills/local-ci/SKILL.md) 及其 references，再在任务容器内启动公司配置的单一 Codex 会话。`agent_ci/policy.py` 通过真实 diff 决定不可删减的最低检查，Skill 规定工作循环；模型可安排顺序、增补验证，并在构建期间开展只读审查。

Codex 新建与恢复会话均使用 `danger-full-access`、`approval_policy=never`，启用原生 Shell、unified exec 和编辑能力。原生探索目录为 `/codex/workspace/candidate/`，包含独立 checkout、venv、可用时的 backend 和 home/tmp/cache/state；源码副本排除可变 Git 元数据，通过来源清单绑定冻结提交。Codex 可以直接写脚本和运行实验，原生命令事件及源码快照保存在宿主私有记录，不作为最低检查通过或阻断归因，也不自动发布到 Gitee。

MCP 服务由可信 Harness 绑定当前任务，Codex 通过它调用 `start_check` 和 `run_custom`。后者包含 diagnostic、reproduction、experiment 三种模式：诊断可在正式检查通过前使用，实验在独立副本尝试修改，只有符合条件的正式复现可建立原始 candidate/base 归因。诊断和实验不能代替最低检查，也不是宿主机 shell 或 Docker 管理入口。

十项基础工具仍在 `tools/`，wheel 构建与安装分开。candidate/base 分别拥有 checkout、venv、构建与可写缓存。Triton 3.0 保留后端、FlagGems 和性能能力；其他版本只提供前端及适用的源码/控制面检查。缺少声明能力属于 infra_error。所有 PR 均需信息校验和架构审查；额外 AI 高风险阻断要求相同复现在 candidate 两次失败、base 通过。结构化结果和生成测试证据来自执行器，模型文字不能代替通过记录。

Codex 的业务决定只有 continue/block。`pass/fail/infra_error` 表示证据结果或失败原因，`publishing/complete` 表示本地上传进度。缺必要证据或恢复预算耗尽时不能生成通过结果；本地 complete 不等于检查通过。运行期不启动多 Agent。

`finish` 封存 result 和证据，Codex 工作结束；上传阶段不持有构建锁。上传失败只重发相同结果，不恢复模型，成功上传 Gitee 后 worker 将任务置为 complete。GitHub 独立校验当前身份和可信 diff 对应最低检查，发布 status、统一 comment 和 Pages，不向 Gitee 写回执。

保持用户确认的 status → comment → Pages 顺序。GitHub Actions 显示发布失败，后续定时任务继续接收重试。两个 Commit Status context 保持原名称；它们表示检查结果，完整发布进度还需查看对应 Actions。没有跨系统完成确认，PR status 成功可早于 Dashboard 更新。

## 权限与运行环境

宿主机普通 CI 用户运行 Harness、Rootless Docker 和用户级 systemd；自动执行不使用 sudo。Docker endpoint 显式固定到该用户的 rootless socket，context 必须匹配，禁止默认 context 或系统 Docker 回退。部署要求 CPU、memory、pids 限额及显式 canary 的实际 cgroup 证据；默认全局一个构建测试任务、MAX_JOBS=8。

每个 attempt 固定 task_id、run_id、容器 ID、镜像 ID 和私有数据卷。容器内 Codex、candidate、base、diagnostic 使用四个不同非 root UID；只有宿主机 Harness 能通过 Docker 管理接口执行容器 UID 0 的准备、取证与清理。模型认证和 MCP token 位于 Codex 私有目录，其他身份不可读取。任务进程使用 no_new_privs；容器不挂载 Docker socket、完整宿主机 state、GitHub/Gitee 凭据或整个 home。

四个身份分别保护会话、候选安装、基线安装和诊断执行，也使 Harness 可以先终止测试进程而保留 Codex 完成封存请求。它们不是四个宿主账号或四个常驻服务。原生命令与 Codex 同身份，可以访问模型认证和当前任务 RPC；正式检查与复现通过其他 UID 执行，不能用这层隔离宣称 Codex 自行启动的程序也无法读取凭据。

长期复用的是经可信来源、精确 LLVM 和配方验证的镜像与依赖缓存。镜像根文件系统、控制代码及可信底座只读，任务写入私有目录；不跨任务复用可写 checkout/venv，不把 PR 容器提交成镜像。每日 timer 错峰构建与验证镜像，新任务才使用新发布的摘要，已有 attempt 不被替换。PR 的新 LLVM 不自动晋升正式镜像，3.0 后端准备失败不降级。

`danger-full-access` 不改变 Linux 文件权限和非 root 身份。Codex 可在实验 venv 安装依赖和任务本地工具；apt、系统库、全局驱动等底座变更需要更新可信镜像配方。

任务完成前确认进程终止并保存执行证据，进程清理失败进入 `environment_cleanup` 并阻断整体通过。封存后清理认证、停止容器并按策略保留或删除任务数据。未确认停止、身份不匹配和清理失败进入健康异常。旧常驻环境的设备复用检查不在任务容器路径中；3.0 后端能力通过可信镜像验证和正式任务检查确认。

## 协议、证据与恢复

| 位置 | 内容 |
| --- | --- |
| Gitee `local-ci-control/tasks/<task_id>.json` | 不可变 v4 任务，身份包含 worker SHA、元数据摘要及 full 请求 |
| `current/<subject_digest>.json` | 当前 PR 或分支有效任务 |
| `cancel/<task_id>.json` | 取消原因与可选替代任务 |
| Gitee `local-ci-results/runs/v4/<task>/<run>/` | 封存结果、基础检查及生成测试证据 |
| `local-ci-results/retention/v4/<task>/<run>.json` | 过期结果的身份、摘要与时间，防止误用更旧结果 |
| 宿主机 `state_dir` | journal、execution、outbox、镜像/attempt registry、lease、会话身份及归档证据 |
| 任务私有数据卷 | 当前 attempt 的 candidate/base 环境、诊断/实验目录与 Codex 私有会话 |

声明式 schema 位于 `agent_ci/schemas/`；运行时还检查摘要、ref 安全、仓库白名单、merge parents、精确 LLVM、依赖 execution ID 和当前任务。JSON schema 合法本身不证明任务可信。

自动重启从 journal 恢复，先处理旧进程和 lease。原 attempt 的容器、镜像、卷与归属仍能验证，且检查依赖未失效时，才复用已有成功项并按身份校验恢复 Codex 会话。容器丢失或新建 attempt 后，旧执行历史保留，依赖旧安装环境的通过记录失效，重新准备和验证；新容器开启新 Codex 会话读取恢复上下文。即使使用同一镜像，也不能把旧容器内安装状态视为仍存在。

OOM 自动降低并行度重试一次，API/网络重试有上限。显式 `worker.py --resume TASK_ID` 与 worker 共用独占锁，须先停止服务。已上传 infra_error 可创建新 run；待上传任务继续原 outbox，不再次构建或调用模型。重复投递同一任务不会强制重建。

日志与证据在可回收数据删除前归档到宿主机，封存目录保持不可变。容器丢失不抹除已有归档证据，尚未导出的内容不能作为完整证据使用。失败/待恢复 scratch 默认保留 24 小时，受 100 GiB 逻辑预算约束；活动或未确认停止的任务不可删。该预算不包含持久证据与 outbox，state 空闲不足时阻止新构建但继续已有上传。

Gitee 结果默认按上传 Git 提交时间保留 30 天，独立 retention timer 删除过期 run 并保留过期标记。清理不等待 GitHub 确认，不回退展示更老结果，也不删除本地 outbox。

## 部署、迁移与监控

配置模板故意留空实际公司镜像、模型目录、后端与中转来源；预检要求补齐，不能猜测设备命令、厂商依赖或模型地址。普通 CI 用户、subuid/subgid、Rootless Docker、user systemd、cgroup controller delegation，以及 3.0 的驱动、设备权限和依赖服务由管理员按实际机器准备。安装器只写用户级 unit 并 daemon-reload，不自动启动或启用服务。

部署要求与任务 worker_revision_sha 一致的干净控制 checkout。迁移是独立离线操作：停止旧接单和 worker，处理在途计算，checkpoint 与备份旧 state → 准备 Rootless runtime 和可信镜像 → 用 `agent_ci/migrate_state.py` 将终态、未上传 outbox 及封存证据导入独立新 state → 核对控制版本后切换。旧 rootful 容器、lease 和安装状态不直接导入；未知活动任务必须先确认停止或取消。`deploy/migrate.py` 单独记录迁移材料，不代替实际数据库导入或服务切换。

回滚必须使用匹配的控制版本、state、会话和工作区备份，先停止新接单并保存新 outbox；不能只恢复旧通过记录后跳过环境重建。详细命令、证明材料及用户级 unit 回退见 [部署与回滚](../scripts/local_ci/deploy/README.md)。

独立用户级 health timer 发布服务、Rootless runtime、镜像、attempt、磁盘、执行和上传状态；Docker 不可达仍生成错误快照，已有 outbox 不依赖任务容器才能上传。GitHub watchdog 读取心跳并通过 SMTP 发异常/恢复通知，去重状态及待发通知跨运行保存。公开摘要不暴露主机路径、配置和凭据；中转不可达与主机离线分别报告。

GitHub 侧继续使用 `GITEE_RESULTS_REPO_URL`、`GITEE_USERNAME`、`LOCAL_CI_HEALTH_URL`、`LOCAL_CI_WORKER_ID` 变量和 `GITEE_TOKEN` secret；SMTP 使用 workflow 中列出的 `LOCAL_CI_SMTP_*` secrets/变量。私有心跳使用只读 `GITEE_HEALTH_TOKEN`。未配置实际地址时明确失败，不自动拼接公司地址；模型 API 保持只在服务器使用。

`local-ci-fork-approval` environment 必须有实际 required reviewers，缺配置不得放行外部 fork。Pages 使用 `github-pages` environment 和 Actions 发布源；部署工具不创建审批者、填入凭据或调整分支保护。审批检查只读 environment 保护规则，相关 job 使用 `actions: read`。此次任务容器调整不改变 GitHub 事件、门禁、协议或 status → comment → Pages 顺序。

## 当前验收入口

测试代码和生成入口保留在仓库中，报告与原始日志按需输出到仓库外：

```bash
python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/local-ci-task-container-verification
```

输出包含 verification.md、verification.json 和各套测试日志，记录当前源码摘要和外部边界替换范围。历史阶段与提交见 [工作记录](ci_refactor_log.md)；旧报告可从对应 Git 提交读取，当前目录不再保存多轮生成产物。

本机验收不代表公司模型、LLVM/后端真实编译、设备、实际邮件或线上 GitHub/Gitee 已通过验证；部署时仍须执行实际资源和能力预检。历史验证结果只说明对应提交，不能代替当前代码或服务器验收。
