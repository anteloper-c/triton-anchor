# Local CI v4：Codex 主导构建、测试与审查

本目录执行 new_CI.md 顶层设计与已确认决策。GitHub 前置检查及接收实现位于 CI_dev；main 仅路由。服务器只访问 Gitee 中转和公司模型服务。

## 当前执行入口

`bash scripts/local_ci/poll_gitee_and_run.sh --config /opt/local-ci/config.json`

配置示例、预检、安装、轮换、健康发布和回滚见 `deploy/`。配置由服务器维护，不能从 PR checkout 读取。原 `config.env` 不再被执行器自动 source；请按 JSON 示例迁移，模型继续使用专用 CODEX_AI_CI_HOME 中实际的 config.toml/auth.json。

主入口运行 `agent_ci/worker.py`。它验证冻结任务及 merge parents、准备版本环境、启动 Codex，并持久保存状态。Codex 的唯一 Skill 入口为 [skills/local-ci/SKILL.md](skills/local-ci/SKILL.md)：`agent_ci/codex.py` 通过 `agent_ci/skill.py` 显式读取入口，再按入口声明加载 references，随后启动 `codex exec`。最低检查由 `agent_ci/policy.py` 和可信 diff 决定，模型不能减免。

Skill 规定工作方法；Harness（`agent_ci/`）管理任务、状态、权限和生命周期；MCP 提供任务工具接口；`tools/` 执行真实构建和测试。每任务只有一个 Codex 会话，业务决定为 `continue/block`；检查结果与发布状态保持原协议，具体映射见 [Skill 说明](skills/local-ci/README.md)。

每次启动保存只读 `TASK_SKILL.md` 会话快照和任务目录内的 `skill-manifest.json`（入口、文件 SHA256 和整体摘要）。恢复要求任务、公司模型配置及 Skill 摘要一致；缺失入口、引用越界、文件缺失或摘要变化均在模型启动前失败，不回退到旧提示词。旧平铺提示词已删除，历史 `codex_ai/` 中的审查提示词不参与当前驱动加载。

## 基础工具和能力

`tools/run_tool.sh TOOL_ID` 一次执行一项工具。完整参数、依赖和产物见 `tools/README.md`。

Triton 3.0：环境、前端 build、wheel install/import、frontend smoke、backend rebuild、backend smoke/JIT、FlagGems、compile-time、pass profile、IR serialization。

其他已配置版本：仅前四项，以及相关源码/控制面检查；缺少后端能力显示 not_applicable。3.0 环境临时损坏是 infra_error，不能改成不适用。

产品改动按影响范围取最低检查并集；未知或跨模块改动覆盖全部可用工具。程序 Markdown、prompt、schema、运行配置不是纯文档。所有 PR 必须有信息校验和架构审查，额外 AI 高风险阻断必须有实际候选复现与 base 归因。性能执行失败阻断，纯耗时变化只报告。

## 状态、证据与恢复

宿主机 state_dir 下保存 SQLite journal、任务 metadata/policy、工具记录、Codex session 标识和发布 outbox。模型会话/凭据位于单独的 codex_sessions_root，不发布到结果仓库。

任务状态：queued → preparing → running → publishing → complete。`publishing` 仅表示封存结果待上传 Gitee，上传成功即 `complete`。完成表示本地交付完成，`pass/fail/infra_error` 仍分别表示检查通过、失败和未完成。取消和 supersede 保存 cancelled。工具证据记录 SHA、环境指纹、依赖执行 ID、命令、退出码和 artifacts。重复构建使旧下游证据失效；更换环境也不能重用旧通过记录。

发布目录为 `runs/v4/<task_id>/<run_id>/`。result.json 封存后不可修改，Codex 到此结束；上传失败只重发原有结果，Harness 保留 outbox 并在后续轮询重试，不再调用模型。GitHub 独立读取、校验并发布结果，没有 Gitee 回执和本地等待。保留原有 status → comment → Dashboard 顺序，GitHub 发布失败由 Actions 显示并由后续定时任务重试；因此 PR status 成功不单独证明 Dashboard 已更新。旧 schema 只用于历史读取，不能满足 v4 门禁。

显式续跑：先停止 worker，再执行 `python3 scripts/local_ci/agent_ci/worker.py --config CONFIG --resume TASK_ID`，随后启动 worker。续跑与接单、目录回收共用 poll.lock，避免新 run 与旧目录清理并发。已上传的 infra_error 可开启新 run，复用仍有效的通过项；目录已回收或代际改变时重新执行检查；仍待上传则继续原 outbox，不重新构建。服务重启自动接续未封存任务，已上传的旧回执等待状态迁移为本地 complete。

Gitee v4 结果默认保留 30 天，按结果文件的 Git 上传提交时间计算；每日 retention timer 删除过期 run 目录并保留摘要和过期记录。保留周期独立于 GitHub 发布，过期任务显示 expired，不回退发布更老的 run。此清理不改写 Git 历史，也不删除服务器任务证据；长时间接收中断需在到期前修复或调整 `results_retention_days`。

## 常驻环境与权限

环境按版本及精确 LLVM/recipe 指纹管理活动、候选和上一代。每日错峰准备候选，验证后切换新任务；旧任务 lease 释放前不能回收。PR 新 LLVM 候选不自动晋升为正式活动环境。3.0 仍尝试匹配后端，失败阻断；source/archive 必须来自公司可达镜像或本地缓存并验证来源。

默认全局一个构建测试任务、MAX_JOBS=8，环境重建共用资源锁。Codex 专用非 root 账户不具备 Docker 和 journal 写权限；候选代码在常驻容器的任务目录执行。API 凭据与 Gitee 写 token 不进入候选容器。任务描述、源码和生成文件不能授予权限。

任务收尾由 `agent_ci/workspaces.py` 管理：持久登记目录与代际 → 标记环境 dirty → 执行检查 → 回收专用 CI UID 的进程 → 检查共享依赖、公共目录和设备 → 封存证据 → 按策略回收任务目录 → 释放占用。PID1/管理进程使用 root，任务 UID 不能用于任何常驻服务；任务进程设置 no_new_privs，可信 reaper 用 Linux pidfd 确认没有仍运行的子进程。正常退出、取消和重启均执行回收。

复用检查失败会隔离并停止精确代际，未确认停止时阻止新构建；3.0 必须配置真实设备检查。未知公共目录残留不会被盲删，按环境异常处理。检查含有独立时限，MCP finish 的等待上限由这些可信时限计算；进入封存后不能再启动测试。结果中保留 `environment_cleanup`，检查失败不能给出整体通过。

成功封存后删除任务源码、venv、构建和缓存目录，上传只使用宿主机 outbox。失败/待恢复目录默认保留 24 小时，保留目录的逻辑容量预算默认 100 GiB；超预算优先回收较旧的非活动目录，活动任务不可删。日志优先引用已有封存证据，尚未封存的执行证据先归档并落盘。回收使对应通过记录失去复用资格，但不抹除历史事实。该预算不包含持久证据；磁盘不足时告警、阻止新构建并继续尝试上传。配置和运维命令见 `deploy/README.md`。

启动恢复同时登记旧版本遗留的已知任务目录，先在原代际终止孤儿进程，再释放旧租约。无法确认归属的目录保留供检查。目录回收失败、环境隔离和磁盘预算状态进入独立健康快照、Dashboard 与异常/恢复通知。

## 监控

服务器独立 health timer 汇总 worker、容器、磁盘及任务状态并发布心跳。GitHub 定时读取心跳，执行独立离线检查、结果接收和 Dashboard 更新；SMTP 支持异常去重、发送重试和恢复通知。Gitee 无法访问时报告中转不可达，不能直接认定主机离线。

GitHub schedule 有平台延迟，不承诺实时告警；配置心跳过期窗口须容纳发布和调度间隔。公司模型 API 不会迁移到 GitHub。

## 验证与迁移

运行 `python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/local-ci-v4-verification`，输出各套测试结果和可追溯报告。需要 pytest、PyYAML；模型、编译器后端、Docker、Gitee、GitHub 和 SMTP 外部边界由本机 fixtures 模拟，不发送模型请求、邮件或真实仓库写入。

原固定 deterministic→AI advisory 执行路径已退役，旧 Codex/容器入口明确拒绝执行。历史解析器仍用于已有结果读取。迁移顺序：停止旧接单并记录状态 → CI_dev 接收器与 worker → main 路由 → 新 Poller；具体命令及回滚在 deploy/README.md。

本次交付不包含真实服务器、真实 LLVM/后端构建、公司模型、邮件或线上 GitHub 验收；对应部署预检与验收步骤必须在上线时执行。
