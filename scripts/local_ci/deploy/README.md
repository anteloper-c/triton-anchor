# Rootless Docker 任务容器部署

本目录交付部署代码和本机模拟验证，不表示服务器已上线。实际镜像、LLVM/PPL/torch_tpu、设备、公司模型和中转配置必须来自服务器；模板空值会使预检失败，不猜测地址、模型或厂商命令。

`jiwang_ci` 服务器的逐步部署、原生环境配置与日志位置统一维护在 [仅 Gitee 部署手册](jiwang_ci/HANDOFF.md)。本文提供通用安装、更新和回滚参考；本轮服务器操作范围以该手册的“安装后不接单”为准。

## 运行边界

宿主机普通 CI 账号运行 Harness、Rootless Docker 和用户级 systemd 服务。该账号可有人工维护用的 sudo/wheel/admin 组权限；自动任务不使用 sudo，安装器为所有 CI 服务设置 NoNewPrivileges=yes，禁止服务进程通过 setuid 提权。Rootless Docker 使用单独的用户级 daemon 服务，不把 CI 账号加入系统 docker 组。每次任务的容器绑定 task_id/run_id 和已验证镜像摘要，Codex 与候选代码在同一任务容器的不同非 root UID 下运行。默认 identities 为 candidate=11001、base=11002、diagnostic=11003、codex=11004、read_gid=11000、codex_gid=11004；四个 UID 不得重复，Codex 私有组不能与只读共享组相同。它们是容器内身份，不要求新建宿主机 Codex 账号。只有 Harness 可通过 Docker 管理接口执行容器 UID 0 的准备/清理操作。

任务容器不挂载 Docker socket、完整宿主机 state、Gitee/GitHub 凭据或整个 home。公司 Codex config/auth 只进入该任务的 Codex 私有目录，候选/base/diagnostic 身份不能读取。通用诊断 MCP 的能力由可信宿主机 Harness 验证，只作用于当前任务；不是宿主机任意命令或 Docker 参数透传接口。

容器内 Codex 的新会话和恢复会话均使用 `danger-full-access`、`approval_policy=never`，启用原生 Shell、unified exec 和文件编辑。`/codex/workspace/candidate/` 提供绑定冻结提交、排除可变 Git 元数据的源码副本、实验 venv 和私有缓存；原生实验不改写正式 candidate/base 环境。命令事件和源码快照留在宿主私有任务记录，不自动公开到 Gitee；最低检查和阻断复现仍由 MCP 执行并核验。

执行模式由可信 `agent_ci/codex.py` 写入任务配置并用于新建/恢复；不需要改公司 auth.json，也没有 local-ci.json 的 sandbox 开关。公司来源仅提供实际模型、provider 和认证。原生启动上下文给出 `environment_setup` 路径与参数，后端实验按需初始化；不会在 Codex 启动前自动执行可能失败的 envsetup。原生环境独立于正式候选安装，需要时自行在探索 venv 安装候选 wheel。

保留四个 UID 是为了分别保护会话、候选安装、基线安装和诊断执行，且可以清理测试进程而不终止 Codex。它们不需要四个登录账号，不产生四份常驻服务的内存开销。原生命令和 Codex 同身份，能够读取模型认证和任务 RPC；不要把执行候选脚本的原生命令当作凭据隔离边界。容器根文件系统只读且 Codex 非 root，系统包和全局驱动仍通过可信镜像配方准备；任务内可安装 venv 依赖和本地工具。

单向交付保持不变：Codex 封存结果后结束，Harness 上传不可变 Gitee 结果成功即本地 complete；没有 receipt。Docker 故障不应阻止已有 outbox 重试上传或独立健康发布。GitHub 保持 status → comment → Pages，发布失败由 Actions 和后续接收重试处理，不触发 Codex 重跑。

供仅能访问 Gitee 的服务器窗口及用户逐步部署、调试的手册见 [jiwang_ci/HANDOFF.md](jiwang_ci/HANDOFF.md)：按 docs/build.md 对照镜像依赖，优先使用用户放好的 LLVM/PPL 预编译包，说明路径、摘要、版本与配置的对应关系，提供每步检查点和日志排查。范围为依赖与配置准备、部署预检和用户服务安装，安装后不启动接单，不包含 GitHub 配置或 PR 试跑。该目录的配置和凭据模板有意留空实际服务器信息；本 README 的完整运维流程不扩大该交接范围。

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
- `profiles` 仍按环境 profile 索引，记录唯一 name、Triton 版本、精确 llvm_hash、可信来源与错峰 daily_calendar。image 必须是实际基础镜像的不可变 SHA256 引用；不能填写 PR 可变标签，也不能用运行中的 PR 容器制作基础镜像。`branch_profiles` 可将任务目标分支映射到已有 profile，例如 `CI_dev` → `triton_v3.0`；映射只改变环境选择，不改变任务身份、目标分支或冻结 SHA，未映射分支不会隐式回退。
- `workspace_root`、`workspace_container` 保留为可信镜像配方中的逻辑源码根，用于解释依赖来源及重写容器路径，不表示宿主机常驻任务目录或可复用 PR 工作区。实际任务数据由 attempt 私有卷管理。
- LLVM archive 需要来源、sha256 和精确 commit；源码需要公司可达可信 repository。repositories/archives/prepare_commands 只来自受信控制配置。任务不能把自制依赖写回可信缓存。
- Triton 3.0 必须开启 backend，其他当前版本必须关闭。真实 PPL、torch/torch_tpu、后端、FlagGems 路径与依赖缺失属于部署失败；validation_commands 必须调用真实基础工具，不能填 true。新 LLVM 仍必须匹配被测代码声明；任务容器不使用旧常驻环境的 post_task_validation_commands 或设备复用检查。
- `cleanup_timeout_seconds` 默认 60，`management_timeout_seconds` 默认 600；`finish_timeout_seconds` 默认 3600、最大 86300，且须至少覆盖 `3*cleanup_timeout_seconds + management_timeout_seconds + 60`。这些正整数控制任务进程清理、容器管理和整个封存 RPC 的期限，不再按旧公共目录指纹或设备复用检查推算。

Gitee 任务/结果仓库与独立健康仓库均填写实际地址。Model、上传和 health 凭据保存在私有来源中；EnvironmentFile 必须由运行用户所有、权限 600。预检要求 `GITEE_TOKEN` 和 `health_token_env` 指定的健康发布凭据。SMTP 是可选通道，未配置不阻塞部署；部分配置仍报错。GitHub 侧变量、审批规则及 Pages 配置沿用既有 v4 合同，PR 评论和状态发布不依赖 SMTP，部署工具不更改分支保护或审批环境。

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

控制代码不再随依赖镜像发布。管理器从干净的 control_root Git 提交提取运行所需的 scripts、api_contract、envsetup.sh，缓存在 state_dir/environments/control-revisions/<SHA>，只读挂载到 /opt/local-ci/control；验证、任务和证据恢复容器均使用固定快照。不挂载整个宿主 checkout、.git 或私有部署配置。新任务匹配 worker_revision_sha，已有任务保留原快照，不受宿主更新影响；快照仍被任务引用时不要手工删除。

日常更新控制代码后，使用 `python3 scripts/local_ci/deploy/rotate.py --config CONFIG --profile PROFILE --reuse` 复用依赖镜像并执行新版控制代码自检，不强制构建也不触发清理。路由、资源、凭据等宿主配置变更不重建镜像；基础镜像、依赖配方、prepare_commands 或构建引导程序变化仍需构建。验证命令改变仅重新验证。旧版已验证镜像在完整依赖配方一致时可以复用，其内置旧控制代码被只读快照覆盖；新构建不再 COPY 控制代码，临时构建配方也会移除。

镜像管理器保留上一有效发布，必要时可用 `python3 scripts/local_ci/environments/manager.py --config CONFIG rollback --target-branch BRANCH --release-id RELEASE_ID` 选择依赖配方匹配的已验证镜像，并用当前控制代码验证。回退不替换已有 attempt，切换后重新运行资源 probe。

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

worker 的 WantedBy 为 default.target，对实际 rootless docker user service 使用 Wants/After；不使用 Requires/BindsTo 阻断 Docker 故障时的 outbox 上传。health 与 retention 不依赖 worker 存活。确认没有其他 worker 消费同一队列后，由 CI 用户安排启动/启用 worker、health/retention timer 和各镜像 timer；安装器不替代这一切换操作。

unit 回退使用 `install.py --rollback BACKUP` 审阅，再加 --apply，仍仅使用用户级 daemon-reload；备份必须属于当前用户，第三方修改过的 unit 不覆盖。不同版本的 worker 不能同时消费同一生产队列。

## 更新已有任务容器部署

先停止接单并安排旧任务/会话收尾，保留 outbox 和备份；从 Gitee 取得干净控制 SHA，使用 rotate.py --reuse 验证现有依赖，再做资源 probe 和正式预检，最后重启 Worker。仅 unit 定义变化时重新渲染 unit。宿主 Python 进程不会自动热更新，因此不要在 Worker 运行中原地覆盖其代码。任务 worker_revision_sha 必须匹配当前控制 checkout；镜像的 control_revision 仅记录历史构建来源，不要求同步重建。旧会话的 Skill 摘要不匹配时不得强行恢复。具体顺序见 [已有部署更新步骤](jiwang_ci/HANDOFF.md#已有部署如何更新到本版)。

## 保留与独立监控

失败/待恢复任务目录默认保留24小时，task_workspace_retention_hours=0 表示下次回收；task_workspace_max_bytes 默认100 GiB，仅约束任务 scratch 的逻辑字节。活动或未确认停止的任务不删；成功封存的 outbox、日志证据不为凑预算而删除。可信 state 空闲不足则阻止新任务，继续已有上传。清理按任务身份、容器 ID、标签和路径边界执行；失败保留诊断并告警。

原生 CLI 事件和源码快照在 `state_dir/tasks/<task_id>/<run_id>/` 内私有保存，不自动上传，且不受上述 scratch 预算回收。原始事件可能包含敏感输出，索引脱敏不能替代分享前检查。先清理正式测试身份并封存，再停止 Codex、导出原生修改；中断后补导出成功或明确记录数据丢失，才继续回收。封存后的异常报告为运维故障，不修改不可变的已封存结果。具体文件对应关系见 [任务日志位置](jiwang_ci/HANDOFF.md#后续自己调试时从哪一步查起)。

health timer 使用 systemctl --user，读取公共 image/attempt/runtime 状态和只读 journal；即使 Docker 不可达仍生成可发布的错误快照。watchdog 保留健康、队列、上传、目录及隔离异常的记录、去重和恢复机制；公共摘要不复制宿主机路径、配置、凭据或异常全文。SMTP 的 host/from/to/username/password 均为空时禁用邮件（单独的 port/TLS 默认值不启用），输出 `mail_delivery: disabled`，仍维护 active/history/healthy 和 dashboard，但不保留邮件待发送队列，后续启用邮件也不补发已跳过的历史通知。部分配置仍失败，已启用通道发送失败保留队列重试。SMTP 只能来自实际配置，本机测试使用 --mail-outbox，不发送真实邮件。

results_retention_days 默认30天。独立用户级 retention timer 按上传 Git 时间清理 Gitee v4 run，保留身份/摘要/过期标记，不删除本地 outbox、不等待 GitHub 回执、不回退展示更旧结果。

## 验证边界

```bash
python3 -m pytest scripts/local_ci/deploy/tests scripts/local_ci/maintenance/tests -q
```

测试覆盖用户 unit 安装/回退、Rootless endpoint 与 context、实际 cgroup 值解析、证明失效、受控验证容器清理、Docker 故障健康快照及单向交付。仅 Docker/模型/网络/邮件等边界被替换；本机通过不代表公司 LLVM 编译、设备或模型实际可用。
