# Local CI v4：Codex 主导构建、测试与审查

本目录执行 new_CI.md 顶层设计与已确认的任务容器方案。GitHub 前置检查及接收实现位于 CI_dev，main 保留必要事件路由。服务器通过 Gitee 中转收发任务与结果，模型沿用公司实际服务。

## 当前执行入口

`bash scripts/local_ci/poll_gitee_and_run.sh --config CONFIG`

入口运行 `agent_ci/worker.py`。宿主机普通 CI 用户运行 Harness、Rootless Docker 和用户级 systemd 服务；配置由服务器维护，不能从 PR checkout 读取。部署、预检、可信镜像准备、健康发布与迁移见 [deploy/README.md](deploy/README.md)。旧 `config.env` 不再被执行器自动 source；使用 JSON 配置及实际公司 Codex config.toml/auth.json。

Harness 验证冻结任务及 merge parents，选择精确 LLVM 对应的可信镜像，为当前 task/run 创建独立任务容器。Codex 与构建、测试在同一任务容器中，通过四个不同的非 root UID 隔离 Codex、candidate、base 和 diagnostic。宿主机 Harness 掌握 journal、调度、权限、容器生命周期和结果发布；只有它能调用 Docker 管理接口执行容器 UID 0 的准备、取证与清理操作。

Codex 的唯一 Skill 入口为 [skills/local-ci/SKILL.md](skills/local-ci/SKILL.md)。`agent_ci/codex.py` 通过 `agent_ci/skill.py` 显式读取入口和声明的 references，再启动公司配置的 `codex exec`。每任务运行期只有一个 Codex 会话，不启动多 Agent。Skill 规定工作循环，MCP 提供任务接口，`tools/` 执行真实业务检查；最低检查由可信 diff 和 `agent_ci/policy.py` 决定，模型不能减免。

每次启动保存 `TASK_SKILL.md` 快照和 `skill-manifest.json`（入口、各文件 SHA256 及整体摘要）。任务、公司模型配置与 Skill 身份不一致时，在模型启动前失败。缺入口、引用越界或文件缺失不会回退到旧提示词；历史 `codex_ai/` 提示词不参与当前驱动加载。

## 基础工具与诊断

`tools/run_tool.sh TOOL_ID` 一次执行一项基础工具；参数、依赖和产物见 [tools/README.md](tools/README.md)。

Triton 3.0 提供环境与依赖、Frontend build、wheel 安装/import、Frontend smoke、Backend rebuild、Backend smoke/JIT、FlagGems、Compile-time performance、Pass profiling、IR serialization。其他已配置版本仅提供前四项及相关源码/控制面检查，后端项显示 not_applicable。3.0 已声明能力损坏属于 infra_error，不能降级为不适用。

产品改动按影响范围取最低检查并集，未知或跨模块改动覆盖全部可用工具。程序 Markdown、prompt、schema 和运行配置不算纯文档。所有 PR 必须完成信息校验和架构审查；性能执行失败阻断，纯耗时变化只报告。

Codex 通过当前任务 MCP 的 `start_check` 调用基础工具，也可使用 `run_custom` 的三种模式：

| 模式 | 用途与证据边界 |
| --- | --- |
| diagnostic（默认） | 在正式检查通过前诊断环境、源码和失败原因；读取正式 candidate/base 环境，在独立临时目录执行，不满足最低检查或原始 SHA 归因 |
| reproduction | 在已验证的正式依赖上复现问题；源码复现使用受限 source-only 路径，证据绑定 candidate/base 和同一复现脚本 |
| experiment | 在单独副本中尝试修改和验证假设，可继续同一 experiment；不改写正式安装状态，不满足最低检查或原始 SHA 归因 |

通用诊断允许任务内 Python/Bash，不是宿主机 shell、任意 Docker 参数或凭据访问接口。额外 AI 高风险阻断仍要求相同复现在 candidate 两次失败、base 通过。生成代码、命令、退出状态及归因证据由执行器保存，模型文字不能代替执行记录。业务决定只有 continue/block，映射见 [Skill 说明](skills/local-ci/README.md)。

## 镜像、任务容器与权限

长期保留的是按版本、精确 LLVM 和可信配方构建并验证的镜像及可信依赖缓存。每日错峰更新镜像，验证成功后供新任务使用；已运行任务固定原镜像 ID。PR 使用的新 LLVM 不能自动晋升正式镜像。来源必须是公司可达可信镜像、源码或本地缓存，并验证摘要；3.0 后端准备失败仍阻断。

任务容器根文件系统、可信控制代码和依赖底座只读。candidate/base 分别拥有任务私有 checkout、venv、构建产物和可写缓存；诊断及实验使用各自目录。任务不能把修改写回可信镜像或缓存，也不通过提交运行中的 PR 容器生成镜像。每个 attempt 拥有独立容器和数据卷，不跨 PR 复用可写环境。

公司模型配置、认证和 MCP token 只进入任务的 Codex 私有目录；另外三个 UID 不可读取。任务容器不挂载 Docker socket、完整宿主机 state、Gitee/GitHub 凭据或整个 home。自动执行不使用 sudo，任务进程设置 no_new_privs。默认全局一个构建测试任务，MAX_JOBS=8；CPU、内存和进程数量限额由可信部署配置决定，Rootless endpoint 固定且禁止回退系统 Docker。

收尾先确认任务进程已终止、保存执行证据并封存结果，再删除私有认证、停止任务容器，按保留策略清理数据。进程清理失败记录 `environment_cleanup`，不能生成整体通过；未确认停止或数据清理失败进入健康异常。任务容器不执行旧常驻环境的设备复用检查；3.0 的后端能力仍通过可信镜像验证和正式任务检查确认。

## 状态、证据与恢复

宿主机 `state_dir` 保存 SQLite journal、task/policy、工具记录、会话身份、镜像/attempt registry、lease 和 outbox。模型凭据和私有会话资料不发布到结果仓库。

任务状态：queued → preparing → running → publishing → complete。`publishing` 表示封存结果待上传 Gitee；上传成功即本地 complete。完成表示本地交付完成，pass/fail/infra_error 分别表示通过、失败和未完成。取消和 supersede 保存 cancelled。

执行证据绑定 task/run、被测 SHA、镜像与 attempt 身份、环境指纹、依赖 execution ID、命令、退出码和 artifacts。恢复时分两种情况：

- 原容器、镜像、数据卷及归属均能验证，才恢复同一 attempt；依赖和证据仍有效的成功项可复用，原 Codex 会话也须满足任务、模型和 Skill 身份检查。
- 容器丢失、数据已回收或必须更换 attempt 时，保留已归档证据和历史事实，使依赖原环境的通过记录失效，在新任务容器重新安装和检查。新 attempt 启动新 Codex 会话，读取已有上下文，不冒用旧会话或旧安装状态。

服务重启自动接续未封存任务；启动恢复先确认旧任务进程和 lease 的状态。显式续跑须先停止 worker，再执行 `python3 scripts/local_ci/agent_ci/worker.py --config CONFIG --resume TASK_ID`，随后启动 worker。`--resume` 与接单、回收共用 poll.lock。已上传 infra_error 可创建新 run；待上传任务继续原 outbox，不重新构建。

成功任务封存后清理可回收任务数据；失败/待恢复数据默认保留 24 小时，scratch 逻辑容量预算默认 100 GiB。活动或未确认停止的任务不能为满足预算而删除。工具日志与证据在回收前归档到宿主机；容器丢失不删除已有归档记录，但尚未导出的容器内容不能伪造为完整证据。持久证据与 outbox 不计入 scratch 预算；state 空闲不足时告警、阻止新构建并继续已有上传。

## 单向结果发布与监控

结果目录为 `runs/v4/<task_id>/<run_id>/`。`result.json` 封存后不可修改，Codex 到此结束；上传失败只重发原结果，Harness 保留 outbox 并在后续轮询重试，不再调用模型。Docker 故障不应阻断已有 outbox 上传或独立健康发布。

GitHub 独立读取并校验结果，没有 Gitee 回执和本地等待。保持 status → comment → Pages 顺序；发布失败由 Actions 显示并由后续定时接收重试。因此 PR status 成功不单独证明 Dashboard 已更新。旧 schema 仅用于历史读取，不能满足 v4 门禁。

Gitee v4 结果默认保留 30 天，按结果文件的 Git 上传提交时间计算；独立 retention timer 删除过期 run 并保留身份、摘要与过期标记。周期独立于 GitHub 发布，过期结果显示 expired，不回退发布更老 run。此清理不改写 Git 历史，也不删除服务器任务证据；接收长期中断须在到期前处理或调整 `results_retention_days`。

独立用户级 health timer 汇总服务、Rootless runtime、镜像、task attempts、磁盘及执行状态并发布心跳。Docker 不可达也生成错误快照。GitHub watchdog 读取心跳，SMTP 支持异常去重、失败重试和恢复通知；公开摘要不复制主机路径、配置或凭据。中转不可达与主机离线分别处理。GitHub schedule 有平台延迟，告警窗口须容纳发布和调度间隔；模型 API 仍只在服务器使用。

## 验收与迁移

运行 `python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/local-ci-task-container-verification` 将本机报告和日志生成到仓库外；测试代码保留，生成产物不提交。模型、Docker、硬件、GitHub HTTP、SMTP 等外部边界的替换范围以本次报告为准。历史阶段与提交见 [工作记录](../../docs/ci_refactor_log.md)。本机验证不证明实际公司镜像、LLVM/后端、设备、模型、邮件或线上 GitHub/Gitee 已通过验收，上线时须执行部署预检和实际能力验证。

从旧常驻环境迁移是独立离线操作：停旧接单和 worker、处理在途任务、checkpoint 与备份 → 准备普通用户 Rootless runtime 和可信镜像 → 导入终态、未上传 outbox 与封存证据到新 state → 核对 worker SHA 后切换。旧 container ID、lease 和执行通过项不能作为新任务容器状态直接复用；未知活动任务必须先处理。具体导入工具、材料记录、回滚和用户级 unit 操作见 [部署与回滚](deploy/README.md)。

旧 deterministic→AI advisory、常驻可写环境及旧 Codex/容器入口均不作为当前执行路径。历史验收材料可从对应 Git 提交读取，不作为当前任务容器部署依据。
