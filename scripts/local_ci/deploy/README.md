# Rootless Docker 任务容器部署

本目录交付部署代码和本机模拟验证，不表示服务器已上线。实际镜像、LLVM/PPL/torch_tpu、设备、公司模型和中转配置必须来自服务器；模板空值会使预检失败，不猜测地址、模型或厂商命令。

## 运行边界

宿主机普通 CI 账号运行 Harness、Rootless Docker 和用户级 systemd 服务；自动任务不使用 sudo。每次任务的容器绑定 task_id/run_id 和已验证镜像摘要，Codex 与候选代码在同一任务容器的不同非 root UID 下运行。默认 identities 为 candidate=11001、base=11002、diagnostic=11003、codex=11004、read_gid=11000、codex_gid=11004；四个 UID 不得重复，Codex 私有组不能与只读共享组相同。它们是容器内身份，不要求新建宿主机 Codex 账号。只有 Harness 可通过 Docker 管理接口执行容器 UID 0 的准备/清理操作。

任务容器不挂载 Docker socket、完整宿主机 state、Gitee/GitHub 凭据或整个 home。公司 Codex config/auth 只进入该任务的 Codex 私有目录，候选/base/diagnostic 身份不能读取。通用诊断 MCP 的能力由可信宿主机 Harness 验证，只作用于当前任务；不是宿主机任意命令或 Docker 参数透传接口。

单向交付保持不变：Codex 封存结果后结束，Harness 上传不可变 Gitee 结果成功即本地 complete；没有 receipt。Docker 故障不应阻止已有 outbox 重试上传或独立健康发布。GitHub 保持 status → comment → Pages，发布失败由 Actions 和后续接收重试处理，不触发 Codex 重跑。

## 一次性主机准备

管理员确认普通 CI 账号、足够的 subuid/subgid、newuidmap/newgidmap、用户会话/D-Bus、linger、Rootless Docker、cgroup v2/systemd 及 CPU/memory/pids controller delegation。Rootless Docker 使用该账号的 user service；不能用系统 service 加 User= 代替。Docker data-root 使用实际支持的本地文件系统。发行版 user namespace、AppArmor 等要求按实际系统检查，不由任务修改。

3.0 的设备节点、组/ACL、驱动与 torch_tpu/PPL/runtime 的匹配版本、宿主机守护服务或 socket、设备恢复命令须逐项确认。不能以普通容器启动成功代替真实后端验收，也不能擅自配置 privileged、系统 Docker fallback 或全局设备放行。公司 CA、DNS、代理及可达镜像/LLVM 来源沿用实际配置。

参考：[Docker Rootless 前提](https://docs.docker.com/engine/security/rootless/)、[用户服务和资源限制](https://docs.docker.com/engine/security/rootless/tips/)、[发行版与运行限制](https://docs.docker.com/engine/security/rootless/troubleshoot/)。管理员准备不在自动任务中执行。

## 私有配置

使用 config.example.json 的 schema `triton-anchor-local-ci-config/v2`，填写实际值，私有 JSON/EnvironmentFile 不提交。

- `runtime.kind=docker-rootless`；`runtime.endpoint` 必须显式指向当前 CI UID 在 /run/user 下的私有 socket；`runtime.context` 必须指向同一 endpoint；`runtime.service` 是实际 Rootless Docker user unit。每条 Docker 调用固定 --host，拒绝 /var/run/docker.sock、其他 UID 的 socket 和默认 context 回退。
- `resources.cpus/memory_bytes/pids_limit` 部署必填且为正数，后两项为整数；`max_jobs` 默认仍为 8。仅写 Docker 参数不算资源约束已生效，须通过下面的显式验证。
- `state_dir` 是普通 CI 用户所有的独立可信目录；`rpc_socket_dir` 使用该用户运行时目录。Codex 会话保存在每个任务的私有 named volume，不再配置独立宿主 `codex_sessions_root`。control_root 是完整受信 checkout，生产必须干净并与任务 worker_revision_sha 一致。
- `codex_bin` 是受信镜像内的真实 Codex 绝对路径；`codex_home` 是宿主机现有公司专用 config.toml/auth.json 来源，文件由 CI 用户所有且仅该用户可读。沿用实际 provider/model/auth，不写入 profile.env；不再配置宿主机 codex_user/container_execution_user。
- `profiles` 仍按目标分支索引，记录唯一 name、Triton 版本、精确 llvm_hash、可信来源与错峰 daily_calendar。image 必须是实际基础镜像的不可变 SHA256 引用；不能填写 PR 可变标签，也不能用运行中的 PR 容器制作基础镜像。
- `workspace_root`、`workspace_container` 保留为可信镜像配方中的逻辑源码根，用于解释依赖来源及重写容器路径，不表示宿主机常驻任务目录或可复用 PR 工作区。实际任务数据由 attempt 私有卷管理。
- LLVM archive 需要来源、sha256 和精确 commit；源码需要公司可达可信 repository。repositories/archives/prepare_commands 只来自受信控制配置。任务不能把自制依赖写回可信缓存。
- Triton 3.0 必须开启 backend，其他当前版本必须关闭。真实 PPL、torch/torch_tpu、后端、FlagGems 路径与依赖缺失属于部署失败；validation_commands 必须调用真实基础工具，不能填 true。新 LLVM 仍必须匹配被测代码声明；任务容器不使用旧常驻环境的 post_task_validation_commands 或设备复用检查。
- `cleanup_timeout_seconds` 默认 60，`management_timeout_seconds` 默认 600；`finish_timeout_seconds` 默认 3600、最大 86300，且须至少覆盖 `3*cleanup_timeout_seconds + management_timeout_seconds + 60`。这些正整数控制任务进程清理、容器管理和整个封存 RPC 的期限，不再按旧公共目录指纹或设备复用检查推算。

Gitee 任务/结果仓库与独立健康仓库均填写实际地址。Model、上传、health 和 SMTP 凭据保存在私有来源中；EnvironmentFile 必须由运行用户所有、权限 600。GitHub 侧变量、审批规则及 Pages 配置沿用既有 v4 合同，部署工具不更改分支保护或审批环境。

## 镜像准备、预检与资源实效

先执行只读配置检查，修正全部缺失字段：

```bash
python3 scripts/local_ci/deploy/preflight.py --config CONFIG --configuration-only
```

在普通 CI 用户会话中检查 Rootless Docker，再对每个配置 profile 执行受信镜像构建/验证；以下命令将创建镜像和验证容器，不是安装服务：

```bash
python3 scripts/local_ci/deploy/rotate.py --config CONFIG --profile PROFILE
```

每日 timer 仍使用相同 profile 入口，职责已变为可信镜像更新。验证成功才晋升新镜像，已运行任务固定原摘要；缓存与镜像只按 ownership、引用关系及保留策略回收，不做全局 prune。

可信控制代码构建进镜像并绑定 control_revision，不将宿主 control_root 挂载到任务容器；镜像与任务须匹配控制版本。镜像管理器保留上一有效发布，必要时可用 `python3 scripts/local_ci/environments/manager.py --config CONFIG rollback --target-branch BRANCH --release-id RELEASE_ID` 选择已验证镜像；该镜像必须仍匹配当前 profile、LLVM 和控制配方。回退不替换已有 attempt，切换后重新运行资源 probe。

所有 profile 的受信镜像就绪后，显式运行实际资源验证：

```bash
python3 scripts/local_ci/deploy/preflight.py --config CONFIG --probe-runtime
python3 scripts/local_ci/deploy/preflight.py --config CONFIG
```

--probe-runtime 创建有唯一 ownership 标签的可信验证容器，禁用网络，读取容器内 cpu.max、memory.max、pids.max 并比较实际配置；结束时核对容器 ID 与标签后定点删除。它不执行 PR、调用模型、发邮件或安装服务。本轮只以 Docker 边界替身验证代码，没有在真实服务器执行。

通过记录保存在 state_dir/deploy/runtime-probe.json，权限 600，绑定 endpoint、daemon ID、控制版本、配置、活动镜像和实测限额。普通预检只读取；缺记录或配置/镜像变化明确失败，不假定限制生效。更新镜像或配置后重新运行显式 probe。安装 --apply 必须有匹配记录。仅 cgroup v2/Docker info 合法不足以替代该证据。

## 用户级安装与回退

先渲染审阅：

```bash
python3 scripts/local_ci/deploy/install.py --config CONFIG --credentials-env CREDENTIALS_ENV --render-dir REVIEW_DIR
```

普通 CI 用户确认生产预检通过后给相同命令加 --apply。只向当前用户的 XDG_CONFIG_HOME/systemd/user（默认 ~/.config/systemd/user）写 unit、保存原文件备份并执行 systemctl --user daemon-reload；不 start、不 enable、不改模型配置。root/sudo 执行和系统 unit 目录会被拒绝。

worker 的 WantedBy 为 default.target，对实际 rootless docker user service 使用 Wants/After；不使用 Requires/BindsTo 阻断 Docker 故障时的 outbox 上传。health 与 retention 不依赖 worker 存活。管理员完成旧接单退役后，由 CI 用户安排启动/启用 worker、health/retention timer 和各镜像 timer；安装器不替代这一切换操作。

unit 回退使用 `install.py --rollback BACKUP` 审阅，再加 --apply，仍仅使用用户级 daemon-reload；备份必须属于当前用户，第三方修改过的 unit 不覆盖。旧 v1 系统 unit 备份不能由新用户安装器自动恢复，须按迁移备份由管理员处理旧系统服务。新旧 worker 不能同时消费同一生产队列。

## 从常驻环境迁移

先停止旧接单并处理在途任务；旧 Rootful Docker 和新 Rootless Docker 是不同运行时，不能直接导入旧 container ID/lease 当作新任务容器。

1. 停止旧 worker 后，由其实际管理员显式执行 SQLite checkpoint，使 journal 不再有非空 WAL，再保存旧控制版本、完整 state、封存结果、会话及匹配工作区备份，记录文件摘要和旧容器停止证明。示例为在旧环境中执行 `sqlite3 OLD_STATE/journal.sqlite3 'PRAGMA wal_checkpoint(TRUNCATE);'`，必须检查 checkpoint 成功；不能对运行中数据库操作。离线导入以只读 immutable 模式读取源，不替调用者 checkpoint 或更改旧 schema。
2. 完成普通 CI 用户、rootless/资源实效、可信镜像、公司模型来源和实际 3.0 后端准备。
3. 旧计算任务已停止，结果已上传、已封存待上传或明确取消；旧 lease/容器的状态由其实际管理者核对。未停止的未知活动任务不能作为可迁移对象。确认清单包含 `old_intake_stopped/old_worker_stopped/old_containers_stopped/leases_released:true` 和 tasks 列表；显式取消项记录 task_id、state=cancelled 和 reason。导入器直接读取源 journal 验证 publishing outbox 的封存摘要，无需在 tasks 重复提供摘要。
4. 使用实际离线导入工具，源目标 state 必须分离。先查看计划，再显式应用：

```bash
python3 scripts/local_ci/agent_ci/migrate_state.py --source-state OLD_STATE --target-state NEW_STATE --inventory CONFIRMED_INVENTORY
python3 scripts/local_ci/agent_ci/migrate_state.py --source-state OLD_STATE --target-state NEW_STATE --inventory CONFIRMED_INVENTORY --apply
```

导入保留终态和未上传 outbox/封存证据，避免重复消费 Gitee current 中的已完成任务；旧执行环境通过记录不作为新容器安装状态复用。工具不接管 rootful 容器，不从未知 lease 推断它已停止。未上传结果只重试原封存内容，不重入 Codex。

5. 核对 Gateway 投递的 worker_revision_sha 与新控制 checkout，再由用户级 worker 接单。自动重启从新状态恢复；显式 --resume 必须先停止持有 poll.lock 的 worker。新的运行标识不能覆盖旧封存目录。
6. 分开验证不可变 Gitee 上传与独立 GitHub status/comment/Pages，保留单向完成语义。

migrate.py 是独立的材料记录层，schema 已为 v3；plan/record/status 不执行安装、服务切换或数据库导入。阶段为 compatibility_ready → rootless_ready → image_releases_ready → main_ready → old_intake_stopped → old_tasks_drained → state_migrated → poller_ready → verified。旧 v1/v2 材料不能直接标为新架构迁移完成。

rootless_ready 引用实际 runtime_proof 文件及 SHA256；image_releases_ready 保存 profile/image_id/llvm_hash/validated；state_migrated 保存终态、outbox、执行复用失效、lease 已核对和 rollback_backup 材料。记录层的 old_tasks_drained 允许 publishing，但须提供 execution_stopped/result_sealed=true、result_digest/evidence_path，验证计算已结束且封存 v4 task/run 一致；这份审计材料与导入器直接读源 journal 的验证互补。rollback_backup 为 runtime-backup/v1 清单，列出 control/state/workspace/sessions 四类备份文件路径、摘要及旧 worker SHA。最终 upload/github_publication 继续匹配相同 task_id/run_id/tested_sha/result_digest。

回退先停止新接单、处理新任务容器并保存 outbox，再恢复旧控制版本及其匹配 state/会话/工作区。仅回退 unit 或 SQLite 不会恢复已删除的 venv/checkout。源 state 保持完整是回退依据；没有匹配工作区备份时应重新验证，不能恢复旧通过记录后直接跳过安装。

## 保留与独立监控

失败/待恢复任务目录默认保留24小时，task_workspace_retention_hours=0 表示下次回收；task_workspace_max_bytes 默认100 GiB，仅约束任务 scratch 的逻辑字节。活动或未确认停止的任务不删；成功封存的 outbox、日志证据不为凑预算而删除。可信 state 空闲不足则阻止新任务，继续已有上传。清理按任务身份、容器 ID、标签和路径边界执行；失败保留诊断并告警。

health timer 使用 systemctl --user，读取公共 image/attempt/runtime 状态和只读 journal；即使 Docker 不可达仍生成可发布的错误快照。watchdog 保留健康、队列、上传、目录及隔离异常的通知去重、发送重试和恢复机制；公共摘要不复制宿主机路径、配置、凭据或异常全文。SMTP 只能来自实际配置，本机测试使用 --mail-outbox，不发送真实邮件。

results_retention_days 默认30天。独立用户级 retention timer 按上传 Git 时间清理 Gitee v4 run，保留身份/摘要/过期标记，不删除本地 outbox、不等待 GitHub 回执、不回退展示更旧结果。

## 验证边界

```bash
python3 -m pytest scripts/local_ci/deploy/tests scripts/local_ci/maintenance/tests -q
```

测试覆盖用户 unit 安装/回退、Rootless endpoint 与 context、实际 cgroup 值解析、证明失效、受控验证容器清理、Docker 故障健康快照、迁移文件摘要及单向交付。仅 Docker/模型/网络/邮件等边界被替换；本机通过不代表公司 LLVM 编译、设备或模型实际可用。
