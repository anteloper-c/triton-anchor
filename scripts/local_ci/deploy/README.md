# Local CI v4 部署交付

本轮交付本机模拟验证和可部署代码，不执行真实服务器上线，不覆盖现有模型、容器或服务配置。模板中的空镜像、依赖来源、账号和通知字段需填写服务器实际值；预检明确失败，不猜测后端镜像或改用其他模型。

## 配置与凭据

以 `config.example.json` 建立服务器私有配置，保留实际公司 Codex `config.toml`、`auth.json`。`codex_home` 指向现有独立凭据目录，`codex_bin` 指向实际程序。使用 Python 3.11+；Python 3.10 需安装 `tomli`。JSON 和私有 EnvironmentFile 不得提交。

本版本采用单向交付：Codex 成功封存即结束，Harness 上传 Gitee 成功即本地 complete；GitHub 独立接收并发布，没有回执。旧配置删除 `receipt_timeout_seconds`，否则预检明确失败。上传失败保留 outbox 并按轮询间隔继续尝试，每次 Git 上传最多三次网络尝试；不调用模型。保留原 status → comment → Pages 顺序，GitHub 发布失败通过 Actions 和后续定时接收处理。

`profiles` 按目标分支配置。示例为 `triton_v3.0/3.3/3.6`；若 `main` 也要执行任务，显式为它登记实际版本配方和唯一 profile 名称。3.0 保留后端能力；其他版本不配置 backend、PPL、FlagGems 或性能能力。`container_execution_user` 或 profile 的 `execution_user` 必须填写容器专用 CI 数字 UID，或数字 `UID:GID`；UID/GID 都不得为 0，且 UID 不得用于容器常驻服务。manager 探测实际 UID/GID，工具仅授予当前任务可写目录权限。

宿主机 `codex_user` 由管理员创建，必须非 root 且不属于 root、docker、sudo、wheel、admin、adm、systemd-journal、lxd、libvirt 组，并与容器执行用户使用不同 UID。Codex 通过任务 MCP 调度；可信 worker 持有 Docker 和发布权限。`codex_sessions_root` 必须与 `state_dir` 分离、不互相包含，所有父目录允许该账号遍历；不得向该账号开放可信 state 写权限、部署配置或 Docker socket。可信控制 checkout 的 MCP 脚本与父目录须允许该账号读取/遍历，部署预检会实际以该账号验证。

生产 Gitee URL、基础镜像和依赖镜像由配置提供，Local CI 不使用 GitHub 源回退。预编译 LLVM 使用 `llvm.mode=archive`，配置 `archive` 本地绝对路径或 `url`，以及强制 `sha256`、`commit` 和可选 `strip_components`。源码模式使用 `llvm.mode=source`、公司可达 `repository`，checkout 必须匹配任务 LLVM SHA；补丁要求可信路径和 SHA256。默认 LLVM recipe 包含真实链接依赖 host、NVPTX、AMDGPU；厂商派生工具链使用实际参数和补丁。

新 LLVM 可通过 `llvm.revisions[hash]` 登记匹配制品，或由源码模式自动准备。3.0 仍必须通过后端重建，失败不降级前端。已有容器可显式配置 `existing_container`、`existing_workspace_host` 和真实 `env.LLVM_BUILD_DIR` 导入；manager 检查身份和 workspace mount，不停止或回收导入容器。导入不替代每日轮换所需完整 recipe。

## 常驻环境与每日轮换

每代拥有独立 workspace、依赖树、容器 ID、环境指纹；容器通过 `docker create/start` 建立，持续服务多个任务，不使用任务 snapshot、`docker commit` 或 `--rm`。控制目录只读挂载 `/opt/local-ci/control`，工具只读挂载 `/opt/local-ci/tools`；额外依赖 mount 只读，设备仅来自可信 `devices` 配置。

`repositories` 是目录名到 `{repository, commit}` 的映射，供实际 anchor/backend/FlagGems 镜像按精确提交准备。backend 源码 `BACKEND_PATH` 必须位于当前 workspace 映射内，以便每任务复制独立 checkout。`archives` 可按名称登记 PPL 等预编译包（本地路径或 URL、SHA256、strip_components），安装到 `/workspace/deps/<name>`。`prepare_commands` 为容器内可信 argv 列表，用于现有 venv、runtime 和 Python 依赖；不能携带模型或发布凭据。

每个 profile 必须给出实际 `SEED_PYTHON` 或 `PYTHON_VENV_ACTIVATE`；seed 环境需安装 `build`、`setuptools`、`wheel`、`pybind11`、`PyYAML`、`pytest`。manager 在接单前以真实容器执行用户检查这些导入、LLVM 和可信工具可读性。systemd 使用 `UMask=0077` 保护 journal 和凭据；manager 仅给代际工作区及依赖显式添加读取/遍历权限，保持依赖由 worker 所有且不可由任务用户写入。3.0 的 `BACKEND_WHEEL_PATTERN` 必须填写实际后端 wheel 文件名模式，同时填写后端发现名、smoke/JIT 命令及后端、FlagGems、PPL 路径。

`validation_commands` 是检查名到 argv 的映射，必须有 `environment`、`frontend_build`、`wheel_install_import`、`frontend_smoke`；3.0 还要有 `backend_rebuild`、`backend_smoke_jit`。每项可使用 `["python3", "/opt/local-ci/control/scripts/local_ci/deploy/validate_environment.py", "对应工具名"]` 调用真实基础工具，不能填入 `true` 等空检查。默认 checkout 是配置在 `repositories` 的 `triton-anchor`，可用真实 `ANCHOR_DIR` 覆盖；venv、backend 环境由 profile env 提供。

每日 job 使用全局 `state_dir/resource.lock`，候选全部验证通过才原子晋升。任务 lease 固定代际，晋升只影响新任务。上一可用代际、所有有 lease 的代际保留；其他代际满 `generation_retention_hours` 才按 ownership 标签回收，绝不全局 prune。准备失败停止未使用的候选容器并保留诊断。首次上线先完整验证候选，不能把只准备依赖的环境当作产品验收通过。

LLVM 缓存以工具链配方、LLVM SHA 和实际 image ID 为键，保存完整安装树摘要和原子 ready 标记。代际只复制验证成功的缓存，不挂载共享可写缓存；损坏缓存隔离后重建。环境指纹同时包含实际可信控制 checkout 的 Git HEAD，控制脚本变更会创建新代际，不复用旧工具版本的成功环境。

## 任务结束、环境复用与目录回收

任务在常驻容器内使用独立 checkout、venv 和构建目录。任务进程启用 `no_new_privs`；可信 worker 的受控 root reaper 在确认容器身份后，仅终止专用任务 UID 的进程，再验证没有残留。`cleanup_timeout_seconds` 默认 60，必须为正整数，用于等待清理命令完成；超时或无法确认停止会隔离代际，保留诊断。专用 UID 不与常驻服务或宿主机 Codex 账户共用，是按 UID 清理的部署前提。

每代首次可用时保存公共目录、共享依赖和工具链状态摘要；每次任务结束，先停止任务进程，再比对摘要并运行设备复用检查。3.0 profile 必须提供真实 `post_task_validation_commands`（非空 argv 列表的列表），用于确认设备和后端运行时已恢复可接单状态；样例故意留空，预检会失败，需要填入公司服务器实际命令，不能用 `true` 等占位。`post_task_validation_timeout_seconds` 默认 120，`hygiene_snapshot_timeout_seconds` 默认 600，两者须为 1 至 3600 的整数；前者按 profile 配置，后者为全局共享状态探测超时。其他版本可不配置设备检查。

这些复用检查发生在结果封存前，失败记录 `environment_cleanup` 基础设施错误，不能得到整体通过结果。失败代际进入 `quarantined`，不再接单并尝试停止；停止未获确认时阻止接单、轮换和回滚，并持续保留。已确认停止且没有 lease 的隔离代际，从隔离时刻起满 `generation_retention_hours` 后可回收；不能手改 registry 将其恢复为可用。新任务使用经过验证的可用代际；旧任务仅在其工作目录、安装状态和绑定代际仍有效时复用成功执行记录。

目录回收由 worker 每次扫描执行，不新增服务或 timer。成功结果封存进入 `publishing` 后即可删除任务 checkout、venv 和构建目录，不等待 Gitee 上传；已停止的取消任务也可立即回收。失败或待恢复目录默认保留 `task_workspace_retention_hours=24` 小时，允许 0 至 87600 的有限数值，0 表示下次扫描立即回收；`task_workspace_max_bytes` 默认 107374182400（100 GiB），必须为正整数。超出总预算时，按保留时间优先回收较旧的非活动任务目录；运行中或未确认停止的任务不可删除，保护项仍超预算时健康状态报错并阻止新执行。

预算按逐任务目录的逻辑文件字节统计，包含 base/candidate checkout、venv、构建和临时产物；它不是整个磁盘的硬配额。删除前保存执行日志与证据，已有封存证据优先复用；已封存 outbox、上传所需结果目录和审计记录不在回收范围内，不为满足 scratch 预算而删除它们。`workspace-health.json` 另行报告 `durable_evidence_bytes`、`state_free_bytes` 和 `minimum_free_bytes`；可信 state 所在磁盘低于配置的空闲阈值时阻止新任务执行，已有结果仍继续上传。目录删除后撤销相关成功检查的可复用状态；人工续跑会重新创建独立环境并重跑必要检查。封存后目录回收失败会隔离代际并写入 `workspace-health.json`，保持原结果不可变。

worker 启动持有单例锁后，先在旧 lease 指定的原代际停止遗留进程并验证环境，再释放 lease 或接新任务；普通进程重启恢复无需人工 `--resume`。显式续跑使用 `worker.py --config CONFIG --resume TASK_ID`，需先停止常驻 worker，执行该命令后再启动服务，避免与同任务的目录回收并发；常驻 worker 持锁时该命令返回 2。已封存且只待上传的任务重试原 outbox，不重新调用 Codex，也不依赖被回收的工作目录。

## 安装与回退

控制版本必须完整包含 `scripts/local_ci/skills/local-ci/SKILL.md` 和其 `references/`。预检会实际解析入口及引用；复制部分提示词不能通过。Skill 与 Harness 一起版本化，`tools/` 保持原位置。更新此版本前先停止旧接单并处理在途任务，保存旧控制 checkout、状态和会话；没有 Skill 摘要的旧会话不能在新驱动上继续，应在原可信版本收尾或取消后重新投递。回退使用原控制版本及匹配的任务/会话记录，不能手改摘要混用规则。

将实际私有 EnvironmentFile 导入管理员环境后，执行只读预检，不调用模型、下载依赖或发邮件：

```bash
python3 scripts/local_ci/deploy/preflight.py --config /etc/triton-anchor-local-ci/config.json
```

先渲染审阅 systemd 文件，默认不安装也不启动：

```bash
python3 scripts/local_ci/deploy/install.py --config /etc/triton-anchor-local-ci/config.json --credentials-env /etc/triton-anchor-local-ci/credentials.env --render-dir /tmp/local-ci-units
```

管理员预检通过后给相同命令加 `--apply`。安装保留原 unit 精确备份，绝不修改 JSON、model 配置或容器。随后管理员安排旧 poller 退役，再启动 `triton-anchor-local-ci.service`、`triton-anchor-local-ci-health.timer`、`triton-anchor-local-ci-retention.timer` 和各环境 timer；旧新 worker 不应并行消费生产任务。停旧服务前保存 unit、EnvironmentFile 和在途任务记录。

服务器必须部署与投递任务 `worker_revision_sha` 一致的已提交、干净 CI 控制 checkout；可信脚本有未提交变更或 SHA 不同，worker 会报告基础设施错误。更换控制代码前先停旧接单、核实在途任务已完成或明确取消、再切换 checkout；不能执行任务携带的策略来弥补控制版本差异。

`migrate.py` 提供不执行生产动作的迁移记录：`plan --worker-revision <SHA> --main-revision <SHA> --state <记录.json>` 固定版本，随后通过 `record --state <记录> --phase <阶段> --evidence <证据.json>` 顺序记录兼容 receiver/worker 就绪、main 调度就绪、旧接单停止、旧任务排空、新 poller 与独立监控就绪、最终验收。每阶段保存本地证据路径和 SHA256。旧任务清单必须明确终态、`result_uploaded:true`、匹配本地文件的 `result_digest/evidence_path`，或明确取消原因；运行中和未上传任务不能算排空。迁移记录升级为 v2，不能直接套用旧 v1 的回执证据。

最终验收分别记录 `upload` 和 `github_publication`，两者都指向同一 `task_id/run_id/tested_sha/result_digest`；前者保存上传证据，后者记录 `pages_published/comment_published/github_status_published`。这两份部署验收材料不进入运行期任务状态机，也不要求服务器等待 GitHub。工具仅规划或写本地追溯记录，不停止服务、不部署、不访问远端；填写证据不代替实际执行。

首次启动新版 journal 会在事务中移除旧回执列，将具有成功上传记录的等待任务迁移为 complete；没有上传依据的任务继续待上传或明确 incomplete。先停服务并备份完整 state（包括 SQLite WAL），再升级；回滚旧代码须恢复匹配的数据库及会话备份，不能让旧程序读取已迁移的新数据库。历史 Gitee 回执可留存为旧记录，新代码不读取也不写入。

服务回退执行 `install.py --rollback <backup目录>` 审阅，再加 `--apply`；第三方已修改的 unit 不覆盖。环境回退执行 `environments/manager.py --config <配置> --state-dir <状态目录> rollback --target-branch <分支>`，只切换新任务代际，已有 lease 不变。

启用任务目录回收前，回滚备份应同时覆盖 state 与对应版本工作区；仅恢复旧 SQLite 会留下“通过记录存在、venv 已被新版本删除”的不一致。回退到不识别 `reuse_invalidated` 的旧 Worker 时，必须恢复匹配的工作区快照。没有匹配快照时保留旧 state 作审计，使用独立的新 state/环境重新验证，不能直接复用旧安装通过记录。仅回退 systemd unit 不会恢复已经回收的目录。

## 独立监控

健康 timer 独立于 poller，读取 `health/worker.json`、只读 SQLite、环境 registry、Docker 和磁盘，向已配置 Gitee 健康仓库发布 `worker-health.json`。快照的 `workspaces` 保留 `state_dir/workspace-health.json` 中的目录回收状态、预算和错误；尚未生成时为 `unreported`，损坏或不可读时明确为 `error`。`environments.generations` 同时显示 `dirty/quarantined`、隔离原因、是否确认停止及能否复用，不因 poller 在线而隐藏故障。主机离线由其他机器或 GitHub Actions 的 watchdog 通过快照过期识别。

外部执行 `maintenance/watchdog.py --url <Gitee健康JSON或Contents API地址> --expected-worker <ID> --state <持久incident文件>`；SMTP 从 `LOCAL_CI_SMTP_*` 读取，缺配置明确失败。每次执行都要保存 incident 文件，包括发信失败时的 pending 通知，并在下次恢复该文件；仅供下载的 artifact 不能实现跨执行去重。

模拟使用 `--input <JSON或-> --mail-outbox <目录>` 生成 `.eml`，不发信；`--dry-run` 不写状态。输入支持单 worker 快照或 `{workers: [...], tasks: [...], expected_workers: [...]}`；worker 的 `uploads` 列出未上传 outbox。GitHub 可用 `--tasks-file` 补充尚无结果的队列。默认心跳过期20分钟、未上传等待20分钟、显式进展停止30分钟；上传尝试失败立即告警。上传阶段不误报 Codex 已退出。异常和恢复各通知一次，SMTP失败保留待发通知；不监控 GitHub 回执。

目录回收或预算检查报错触发 `workspace_cleanup_failed`；隔离代际尚未确认停止触发按代际区分的 `environment_quarantine_unconfirmed`。二者沿用 SMTP 去重、发送重试和恢复通知；缺少新的对应健康状态时不宣告恢复。v4 Dashboard 的监控 JSON 附带最小 `worker_health` 摘要，显示目录字节量、状态、任务/代际身份和清理原因代码，不复制原始主机路径、配置、凭据或异常全文。

## Gitee 结果保留

`results_retention_days` 默认 30 天，必须为正整数。独立 retention timer 每日运行 `maintenance/retain_results.py --config CONFIG --apply`；去掉 `--apply` 可先查看清理计划。只清理 `local-ci-results` 分支的 v4 run 目录，年龄依据 `result.json` 的 Git 提交时间（不可变文件的上传时间），不依赖 GitHub 发布或回执，也不删除服务器 outbox。

删除后保留 `retention/v4/<task>/<run>.json` 的身份、结果摘要和过期时间，Dashboard 显示保留期结束，不重新排队或发布更老结果。无效身份、符号链接不会当作可删除结果；并发上传发生时普通 Git push 拒绝覆盖，清理重新读取最新分支后重试。历史 v3 文件保持原状。此操作保留 Git 历史，不能释放历史 blob 占用；如需仓库压缩需另行安排。若 GitHub 接收中断超过保留周期，结果可能过期，应在到期前恢复接收或调整周期。

## 验证边界

```bash
python3 -m unittest discover -s scripts/local_ci/environments/tests -v
python3 -m unittest discover -s scripts/local_ci/maintenance/tests -v
python3 -m unittest discover -s scripts/local_ci/deploy/tests -v
```

测试运行真实 registry、lease、archive、Git fixture、状态迁移、安装回退和邮件 outbox，Docker/SMTP仅在边界替换。真实 LLVM、公司模型、后端硬件和邮件送达需部署时另行验收，模拟结果不表示这些能力通过。
