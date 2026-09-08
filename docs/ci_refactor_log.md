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
