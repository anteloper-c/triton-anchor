# Local CI v4 部署交付

本轮交付本机模拟验证和可部署代码，不执行真实服务器上线，不覆盖现有模型、容器或服务配置。模板中的空镜像、依赖来源、账号和通知字段需填写服务器实际值；预检明确失败，不猜测后端镜像或改用其他模型。

## 配置与凭据

以 `config.example.json` 建立服务器私有配置，保留实际公司 Codex `config.toml`、`auth.json`。`codex_home` 指向现有独立凭据目录，`codex_bin` 指向实际程序。使用 Python 3.11+；Python 3.10 需安装 `tomli`。JSON 和私有 EnvironmentFile 不得提交。

`profiles` 按目标分支配置。示例为 `triton_v3.0/3.3/3.6`；若 `main` 也要执行任务，显式为它登记实际版本配方和唯一 profile 名称。3.0 保留后端能力；其他版本不配置 backend、PPL、FlagGems 或性能能力。`container_execution_user` 或 profile 的 `execution_user` 必须是实际镜像里的非 root 用户；manager 探测 UID/GID，工具仅授予当前任务可写目录权限。

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

## 安装与回退

将实际私有 EnvironmentFile 导入管理员环境后，执行只读预检，不调用模型、下载依赖或发邮件：

```bash
python3 scripts/local_ci/deploy/preflight.py --config /etc/triton-anchor-local-ci/config.json
```

先渲染审阅 systemd 文件，默认不安装也不启动：

```bash
python3 scripts/local_ci/deploy/install.py --config /etc/triton-anchor-local-ci/config.json --credentials-env /etc/triton-anchor-local-ci/credentials.env --render-dir /tmp/local-ci-units
```

管理员预检通过后给相同命令加 `--apply`。安装保留原 unit 精确备份，绝不修改 JSON、model 配置或容器。随后管理员安排旧 poller 退役，再启动 `triton-anchor-local-ci.service`、`triton-anchor-local-ci-health.timer` 和各环境 timer；旧新 worker 不应并行消费生产任务。停旧服务前保存 unit、EnvironmentFile 和在途任务记录。

服务器必须部署与投递任务 `worker_revision_sha` 一致的已提交、干净 CI 控制 checkout；可信脚本有未提交变更或 SHA 不同，worker 会报告基础设施错误。更换控制代码前先停旧接单、核实在途任务已完成或明确取消、再切换 checkout；不能执行任务携带的策略来弥补控制版本差异。

`migrate.py` 提供不执行生产动作的迁移记录：`plan --worker-revision <SHA> --main-revision <SHA> --state <记录.json>` 固定版本，随后通过 `record --state <记录> --phase <阶段> --evidence <证据.json>` 顺序记录兼容 receiver/worker 就绪、main 调度就绪、旧接单停止、旧任务排空、新 poller 与独立监控就绪、最终验收。每阶段保存本地证据路径和 SHA256。旧任务清单必须明确终态、接收确认或取消原因，存在运行中/未回写任务时拒绝记录排空。工具默认仅规划或写本地追溯记录，不停止服务、不发起部署、不访问远端；证据项是维护记录，不能作为真实上线已执行的替代证明。

服务回退执行 `install.py --rollback <backup目录>` 审阅，再加 `--apply`；第三方已修改的 unit 不覆盖。环境回退执行 `environments/manager.py --config <配置> --state-dir <状态目录> rollback --target-branch <分支>`，只切换新任务代际，已有 lease 不变。

## 独立监控

健康 timer 独立于 poller，读取 `health/worker.json`、只读 SQLite、环境 registry、Docker 和磁盘，向已配置 Gitee 健康仓库发布 `worker-health.json`。主机离线由其他机器或 GitHub Actions 的 watchdog 通过快照过期识别。

外部执行 `maintenance/watchdog.py --url <Gitee健康JSON或Contents API地址> --expected-worker <ID> --state <持久incident文件>`；SMTP 从 `LOCAL_CI_SMTP_*` 读取，缺配置明确失败。每次执行都要保存 incident 文件，包括发信失败时的 pending 通知，并在下次恢复该文件；仅供下载的 artifact 不能实现跨执行去重。

模拟使用 `--input <JSON或-> --mail-outbox <目录>` 生成 `.eml`，不发信；`--dry-run` 不写状态。输入支持单 worker 快照或 `{workers: [...], receipts: [...], expected_workers: [...]}`。默认心跳过期20分钟、ACK过期20分钟、显式进展停止30分钟，可通过CLI按真实时延配置。异常和恢复各通知一次，SMTP失败保留待发通知。

## 验证边界

```bash
python3 -m unittest discover -s scripts/local_ci/environments/tests -v
python3 -m unittest discover -s scripts/local_ci/maintenance/tests -v
python3 -m unittest discover -s scripts/local_ci/deploy/tests -v
```

测试运行真实 registry、lease、archive、Git fixture、状态迁移、安装回退和邮件 outbox，Docker/SMTP仅在边界替换。真实 LLVM、公司模型、后端硬件和邮件送达需部署时另行验收，模拟结果不表示这些能力通过。
