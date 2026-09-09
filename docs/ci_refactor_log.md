# CI v4 重构工作记录

依据：new_CI.md、配套流程图、已确认实施计划。范围：本地 CI_dev/main；不推送、不部署服务器、不实际调用模型或发邮件。

| 阶段 | 工作与可追溯证据 |
|---|---|
| 初始核验 | CI_dev 基线 2d4728a18f11f7fd918b03e1cd15c7701c8dbb0c；main 基线 ae8596a28c8287508f0492c23905e7281a111d34；两工作区初始干净。 |
| 顶层差异 | 核对出前置检查未串联、AI advisory、一次性容器、发布失败重跑、缺取消/ACK 等差异；按新方案替换。 |
| GitHub | 类型化 PR 模板、顺序门禁、fork 审批、冻结投递、取消、结果接收、Dashboard 与回执。 |
| Codex/工具 | 新任务协议、可信最低检查、MCP、独立十工具、会话恢复、证据门禁；旧临时容器入口禁用。 |
| 环境/运维 | 常驻代际、LLVM 来源验证、lease、轮换回退、systemd、心跳和 SMTP outbox。 |
| 本机测试环境 | WSL 系统缺 pytest/ensurepip；仅将测试依赖安装到 /tmp/triton-anchor-ci-v4-deps，未改公司模型或服务器配置。 |
| 集成修复 | 实际 Worker→Git relay→接收器→完整 ACK 闭环；补齐控制代码 SHA/脏工作区校验、依赖重建使旧证据失效、独立 UID/venv、发布恢复会话、非 root 目录权限和缺目录的 main 契约检查。 |
| 静态核验 | 使用与 Basic CI 一致的 Ruff 0.15.22（安装到 /tmp/triton-anchor-ci-v4-lint），仓库 Python 阻断级规则通过；部署示例缺配置预检按预期返回失败。 |
| 回归迁移 | 旧 snapshot/拒绝所有 LLVM 升级等断言由 v4 行为测试替换；历史结果解析测试保留。 |
| 验收 | agent_ci/verify.py 完整运行：8 套、421 项通过；Python/shell 语法、Ruff 0.15.22 阻断规则、新模块 F/E9、git diff --check 通过。报告见 ci_v4_verification/。 |
| main 本地提交 | 4fc20aecb82113ca94c2043710cc6970b222eedf：仅统一路由、PR 模板和旧重复入口清理。未推送。 |

未验证：真实公司模型响应、LLVM/厂商后端编译、硬件/仿真运行、真实邮件、线上 GitHub/Gitee 回写及服务器部署。所有 fixture 日志明确标为 simulation。

此前本地 CI_dev 实施提交：`93f869f6a7523d1b6acb129886da552c24133425`；main 调度提交：`4fc20aecb82113ca94c2043710cc6970b222eedf`；交付记录提交：`32fd5026c5a180655f7fd094f3fcbe9c8132225f`。各项要求、实现与测试证据对应关系见 [此前验收覆盖](ci_v4_verification/coverage.md)，机器可读提交记录见 [commits.json](ci_v4_verification/commits.json)。

## 2026-09-08：直接迁入最终 Skill 结构

本轮基于 CI_dev `32fd5026c5a180655f7fd094f3fcbe9c8132225f`；main 保持 `4fc20aecb82113ca94c2043710cc6970b222eedf`。依据新方案、文件对照附件和用户明确给出的最终目录实施，不采用延期迁移；方案中的历史远端链接只作为文档内容，没有访问受禁仓库。

| 要求 | 变更与证据 |
|---|---|
| 唯一入口与完整迁移 | 新增 `skills/local-ci/SKILL.md`、4份 references 与 README；删除旧3份平铺提示词，统一当前说明和测试引用。 |
| 显式加载与可恢复 | 新增 `agent_ci/skill.py`；CodexDriver 加载入口声明的依赖，记录文件摘要和只读快照，恢复校验同一 Skill；部署预检检查完整包。 |
| 最终工作规则 | Skill 明确运行期单 Codex、业务决定 continue/block、Basic→API→Security、3.0后端性能及其他版本前端能力；保留最低检查和证据协议。 |
| 保留正确代码 | Worker、状态、发布、业务 tools、环境管理、GitHub/main 工作流不重写；提示词与真实业务工具分开维护。 |
| 已披露参数漏洞 | MCP/RPC 严格校验参数；公开 start_check 移除内部 custom；自定义执行不能冒充基础工具，历史污染记录不能满足必检。 |
| 验收 | 完整8套回归442项通过（新增21项），Python/shell语法、Ruff阻断规则及新增模块F/E9、Skill结构和差异检查通过；详见 [本轮报告](ci_skill_verification/verification.md) 和 [要求—证据对应](ci_skill_verification/coverage.md)。 |
| 运行边界 | 真实MCP/Unix socket/工具契约与本地Git回执链路通过；模型、Docker环境及GitHub/Pages使用fixture。没有真实模型、编译后端、硬件、邮件或线上部署验收，没有远端推送。 |

以上变更与验收材料随本轮同一个 CI_dev 本地提交保存，可用 `git log -1 -- docs/ci_refactor_log.md` 定位提交；main 无新增改动。

## 2026-09-08：取消 GitHub 回执，按单向交付完成任务

依据：用户明确同意取消反向回执，实施中进一步确认保持 status → comment → Dashboard 顺序。本轮基于 CI_dev `316eb31ab5e1ea187eb7f6eceaa2ef5413f0da05`；main 仍为 `4fc20aecb82113ca94c2043710cc6970b222eedf`。

| 要求 | 变更与证据 |
| --- | --- |
| 拆分完成条件 | Codex 成功封存后结束；Harness 成功上传 Gitee 后 complete；GitHub 独立校验发布。上传失败只重传原 outbox，不再恢复模型。 |
| 删除反向链路 | 删除 receipt schema、PublicationSupervisor、retry_publication MCP、GitHub ack 与回执监控。旧 SQLite 只做一次兼容转换；完整状态备份/回滚要求进入部署文档。 |
| 保持 GitHub 顺序 | 不改前置门禁和 main 调度；继续 status → 幂等 comment → Pages。GitHub 状态按结果摘要去重，评论失败仍可修复，发布异常由 Actions 显示并重试。 |
| 固定保留与监控 | 增加默认 30 天 Gitee run 保留及每日 timer，保留过期身份摘要；健康快照监控上传、队列和恢复，不监控回执。 |
| 同步文档与验收 | 更新唯一 Skill、当前说明、配置预检、迁移 v2 与回滚材料；完整 8 套 472 项通过，Ruff F/E9、Python/shell 语法、Skill 校验与差异检查通过。见 [单向交付报告](ci_oneway_verification/verification.md) 和 [覆盖说明](ci_oneway_verification/coverage.md)。 |

本轮代码、文档和验收材料在同一 CI_dev 本地提交中记录，用 `git log -1 -- docs/ci_oneway_verification/coverage.md` 定位。没有推送、服务器部署、真实模型调用、真实后端编译或邮件发送。此前两版验收作为历史记录保留，回执相关内容不再代表当前实现。

## 2026-09-08：落实常驻容器收尾与任务目录回收

依据：用户要求落实任务后进程清理、证据封存、工作目录回收、环境复用检查和失败隔离。基于 CI_dev `8fe195545b50b16f7c420741b30c1e112da78f63`；main 无改动。

| 要求 | 变更与证据 |
| --- | --- |
| 避免 venv/构建目录堆积 | 新增 TaskWorkspaces 与持久目录状态；成功任务封存后回收，失败默认 24 小时/100 GiB 逻辑预算，活动目录受保护。上传只用 outbox。 |
| 真实清理与复用检查 | 专用 UID、no_new_privs、pidfd 回收与复查；dirty → 共享文件/公共目录/设备检查 → 可复用，失败隔离并核验容器停止。时限由可信配置控制。 |
| 证据与恢复不丢失 | 优先引用封存证据，必要归档逐文件和目录 fsync 后释放租约；中断日志补登记，旧目录/租约恢复，删除前使通过记录失效；同配方换代必须重验。 |
| 生命周期与并发 | 封存启动后禁新执行；收尾检查纳入结果；续跑与 Worker 共用 poll.lock，资源回收与环境轮换共用资源锁。只发布阶段不重建、不调用模型。 |
| 部署与运维 | 增加配置预检、目录/代际健康与既有 Dashboard 摘要、异常去重/重试/恢复通知；回滚需匹配 state 与工作区快照。GitHub workflow、main 及发布顺序未改。 |
| 验证 | 完整 8 套 532 项通过（相较上轮新增 60 项）；Python/shell 语法、Ruff F/E9、差异检查通过。见 [验收报告](ci_cleanup_verification/verification.md) 和 [覆盖对应](ci_cleanup_verification/coverage.md)。验收后仅补充部署 README 的回滚说明，交付摘要单独记录。 |

本轮变更与报告在同一 CI_dev 本地提交中保存，可用 `git log -1 -- docs/ci_cleanup_verification/coverage.md` 定位。模拟运行真实控制、状态、文件和 Linux 子进程逻辑；未运行真实 Docker、LLVM/后端、厂商设备、公司模型、邮件或线上验收，也未推送或部署服务器。

## 2026-09-09：按方案③改为 PR 独立任务容器

依据：用户明确选择每 PR 一次性任务容器，并接受容器内隔离执行身份后要求执行。此决定替代此前常驻容器约束；保留唯一 Skill、十工具、单一 Codex、单向上传、公司模型配置及 GitHub 发布顺序。基线 CI_dev `189284df8827fae3aec6d2476730baae6bebf572`；main 无新增修改。

| 要求 | 变更与证据 |
| --- | --- |
| 专用用户与任务容器 | 宿主普通 CI 用户 Harness，固定 Rootless endpoint；可信镜像只读，每 attempt 独立容器和数据/会话卷，四个非 root UID。生产管理 helper 使用固定操作，无宿主任意命令入口。 |
| Codex 分析和诊断 | 同容器启动公司 Codex，经任务 MCP 调度；新增只读诊断、正式复现和可写实验模式，保留生成脚本及执行事实；更新 Skill references。 |
| 可恢复且不串用 | 同 attempt 恢复、换 attempt 重验；取消按 UID/pidfd 回收。补齐创建到登记间崩溃、卷内产物取证、失去容器、归档和 GC 的路径。封存后异常不能覆盖结果；Docker 故障不阻断已封存 outbox 上传。 |
| 镜像与运维 | 可信 LLVM/依赖来源及摘要、资源必填、用户级服务/错峰镜像 timer/健康/保留 timer；移除旧常驻设备复用配置，缺实际公司配置时预检失败。 |
| 迁移与回退 | 离线导入实际 SQLite 终态/outbox/封存证据，源只读、目标独立、摘要及冲突校验、旧执行状态不复用；提供 user unit 安装回退和部署步骤。 |
| 验证 | 真实本地 Git/SQLite/子进程/四 UID 文件权限测试，加外部边界模拟。测试数量、各套日志和源码摘要统一见 [本轮验收](ci_task_container_verification/verification.md)；要求映射见 [覆盖说明](ci_task_container_verification/coverage.md)。 |

所有变更与报告在同一 CI_dev 本地提交中保存，用 `git log -1 -- docs/ci_task_container_verification/coverage.md` 定位。本轮不推送、部署服务器或发邮件；真实 Docker、公司模型、LLVM/厂商后端、仿真及线上发布仍须部署验收。
