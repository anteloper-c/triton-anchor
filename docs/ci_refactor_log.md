# CI 重构工作记录

依据：new_CI.md、配套流程图及用户后续确认。当前方案见 [架构说明](ci_v4_implementation.md)，服务器步骤见 [部署与调试手册](../scripts/local_ci/deploy/jiwang_ci/HANDOFF.md)。本文件只保留变更与验证摘要，详细差异和当时的报告查对应 Git 提交。

初始基线：CI_dev `2d4728a18f11f7fd918b03e1cd15c7701c8dbb0c`，main `ae8596a28c8287508f0492c23905e7281a111d34`。实施范围仅本地允许分支，未执行远端推送、服务器部署、真实模型调用或邮件发送。

| 提交 | 变更 | 当时的验证记录 |
| --- | --- | --- |
| CI_dev `93f869f`；交付记录 `32fd502` | 初始 v4：串行门禁、任务协议、Codex 调度、十工具、状态恢复、环境管理与运维。此时仍有常驻环境和回执。 | 8 套模拟测试 421 项；Python/shell 语法、Ruff、差异检查。 |
| main `4fc20ae` | 必要事件/定时路由、统一 PR 模板，清理重复入口。 | 随初始 v4 验证相关路由契约。 |
| `316eb31` | 唯一 local-ci Skill 入口和显式加载；MCP 参数与证据约束。 | 8 套模拟测试 442 项，Skill/静态检查。 |
| `8fe1955` | 取消 Gitee 回执；封存后只上传，上传成功即本地 complete；保留 GitHub status→comment→Pages 顺序。 | 8 套模拟测试 472 项。 |
| `189284d` | 当时常驻环境的进程清理、工作目录回收、失败隔离与证据保留。 | 8 套模拟测试 532 项；现行容器方案已替代共享环境复用。 |
| `94650fc` | 改为普通 CI 用户管理 Rootless PR 独立任务容器；容器内 Codex、诊断、执行身份；镜像准备和用户服务迁移。 | 8 套模拟测试 581 项，静态与 Skill 检查；外部 Docker/模型/网络使用替身。 |
| `0266833` | jiwang_ci 部署材料；允许人工 sudo 组，自动服务使用 NoNewPrivileges。 | 部署回归 50 项、Ruff、差异检查；未重跑整套 581 项。 |
| `9243124` | 收窄为仅 Gitee 的部署交接，安装后不接单，去除 PR 试跑与 GitHub 配置。 | 文档链接、Bash 语法、差异检查。 |
| `95bf728` | 按 docs/build 补逐步部署/调试说明；LLVM 使用本地 archive，部署窗口按用户路径计算摘要并确认版本。 | 文档/Bash/JSON 检查，空模板预检按预期失败。 |

2026-09-09，按用户准备提交仓库的要求：删除五轮模拟验证的 58 个报告/日志文件，补充生成目录忽略规则，修正当前文档引用，并压缩本记录。保留测试代码、报告生成器、当前架构和部署材料。核验删除范围、历史提交可追溯、文档链接、忽略规则与 git diff --check；未改运行代码或重新执行完整模拟测试。本次清理提交可用 `git log -1 -- docs/ci_refactor_log.md` 定位。工作区原有的旧说明删除未纳入本次提交。

需要新报告时，在项目根目录运行（需已有测试依赖）：

```bash
python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/local-ci-task-container-verification
```

历史报告从 Git 读取，例如 `git show 94650fc:docs/ci_task_container_verification/verification.md`；这只是历史对象路径，不是当前工作区链接。以上测试数分别对应当时的提交，不累计、不代表当前服务器已验收。真实 Docker/Rootless 配额、公司模型、LLVM/后端、仿真性能、SMTP 和线上 GitHub/Gitee 链路仍需实际环境验证。
