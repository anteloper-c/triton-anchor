# 单向交付验收覆盖

依据：用户确认取消 GitHub → Gitee 回执，并进一步要求保留原有 status → comment → Dashboard 发布顺序。任务、结果继续使用 v4 身份；迁移记录升级 v2。变更基于 CI_dev `316eb31ab5e1ea187eb7f6eceaa2ef5413f0da05`，main `4fc20aecb82113ca94c2043710cc6970b222eedf` 不改动。

| 要求 | 实现及验证证据 |
| --- | --- |
| Codex 到封存结束 | Skill 入口及 AI_CI_PROGRAM 去掉发布恢复；codex.py 拒绝启动已封存任务并回收已结束工作进程；test_codex 的 sealed 两例、MCP 拒绝 retry_publication。 |
| 上传后本地完成 | Worker、Journal 以 Gitee 上传为边界；test_agent_ci 的 upload_completes / code_failure_delivery；实际 Skill/MCP/基础工具链 test_skill_flow 确认先 complete、后 GitHub collect。 |
| 失败只重传 | test_agent_ci 与 test_skill_flow 的 publication_failure、lost/uncertain upload response：同 run、同结果字节、无新模型调用或构建；重启继续 outbox。 |
| 未完成不误报通过 | infra_error 上传后只是交付 complete；显式 resume 建立新 run 并复用有效证据。通过结果不能由模型自报；架构和 PR 信息失败闭环保留。 |
| 移除回执 | 删除 publication.py、receipt schema、MCP 发布接口及 receiver ack；用本地 bare Git 验证结果接收不改控制分支、无 receipts 文件。旧 journal 回执列仅在迁移时识别并删除，不读取内容。 |
| GitHub 顺序保持 | gateway 与 Skill flow 用例验证 status → comment → Pages；Pages 失败时 PR 原成功状态保留、本地任务不重开，后续 collect 重建 Dashboard。 |
| 独立重试与过期防护 | GitHub 根据两 context 最新状态及结果摘要去重，评论仍独立幂等修复；新 head、错误身份及取消结果拒绝回写。最新 run 过期后不回退到旧结果。 |
| 上传和排队监控 | health 输出 uploads，watchdog 监控待上传、失败、排队及恢复；不等待或监控回执，不在 publishing 阶段误报 Codex 退出；显式恢复的新 run 不继承旧 run 上传等待时间。 |
| 固定保留期 | 默认 30 天独立 timer；test_result_retention 用真实本地 Git 验证 dry run、过期删除、近期及历史文件保留、摘要标记、幂等性和无效身份保护。并发新上传受保护，过期 run 的同摘要重传不重建目录，异摘要重传拒绝。Git 历史不重写。 |
| 部署与回滚 | 配置删除回执超时项、验证保留期；旧任务排空基于上传证据；迁移验收分别核对上传与 GitHub 发布，旧 v1 证据不能混用。数据库迁移前备份与回滚说明已更新。 |

完整测试数、源码摘要和逐套日志见 [verification.md](verification.md) 与 [verification.json](verification.json)。另执行 Ruff F/E9、Python/shell 语法、Skill quick_validate 和 git diff --check。

真实执行：调度、MCP stdio/Unix socket、状态存储、环境/契约基础工具、生成脚本、子进程退出、Git task/result 上传及保留期代码。仅在临时本地 bare 仓库操作 Git；模型响应、Docker/LLVM 探测、GitHub HTTP、Pages 和 SMTP 使用 fixtures。

未执行：公司模型 API、真实前后端编译、厂商运行环境和硬件、邮件发送、线上 GitHub/Gitee 写入及服务器部署。模拟通过不代表上述环境已验收。没有远端推送，也没有操作 CI_dev_forPR 或受禁仓库。
