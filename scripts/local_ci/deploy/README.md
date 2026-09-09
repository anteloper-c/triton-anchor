# Local CI 部署

本页说明服务安装；完整链路、工具选择和故障恢复见 [CI 指南](../../../docs/ci_guide_zh.md)，配置字段见 [config.example.json](../config.example.json)。模板存在不代表服务、真实后端或远端通知已经部署。

## 准备主机

生产服务使用持久 Linux 主机、systemd、Docker、Git 和 **Python 3.11 或更新版本**。受信宿主 Poller、health 和 maintenance 以 root 运行：它们本就通过 Docker 管理 root 容器进程，且需要读取由不同容器 UID 写出的证据。候选工具仍以 UID 1000、Codex 以 UID 1001 运行；宿主 root 身份不传给这两个执行入口。

| 主机路径 | 所有者与用途 |
| --- | --- |
| `/opt/anchor-ci` | root 所有的精确 `ci_repo` checkout；运行期间不修改，容器只读挂载 |
| `/etc/anchor-ci/config.json` | root 所有、`0600`；设置真实 profile 和 `local_acceptance=false` |
| `/etc/anchor-ci/service.env` | root 所有、`0600`；systemd 读取 Gitee 凭据和 `DEEPSEEK_API_KEY` 环境变量 |
| `/etc/anchor-ci/recipes/` | root 管理的镜像配方，不能指向 PR workspace |
| `/var/lib/anchor-ci` | root 所有、`0700`，配置为 `state_dir` |
| `/srv/anchor-ci/workspace` | root 所有，配置为 `workspace_host`；仅给容器 UID 必要的目录穿越权限 |

先由维护者安装 root 所有的控制 checkout 和配置文件，并准备目录：

```sh
sudo install -d -o root -g root -m 0700 /etc/anchor-ci /etc/anchor-ci/codex /etc/anchor-ci/recipes /var/lib/anchor-ci
sudo install -d -o root -g root -m 0711 /srv/anchor-ci /srv/anchor-ci/workspace
sudo chown root:root /etc/anchor-ci/config.json /etc/anchor-ci/service.env
sudo chmod 0600 /etc/anchor-ci/config.json /etc/anchor-ci/service.env
```

不要将普通用户拥有的 checkout 直接作为 root 服务控制目录；由 root 管理该精确 checkout，Git 所有者一致，无需通配 `safe.directory`。凭据只存在宿主配置文件中，不写入镜像、Git URL 或仓库。

各 profile 使用不同的固定容器名，共用以下挂载约定：

```text
主机 /opt/anchor-ci/scripts/local_ci -> 容器 /opt/anchor-ci（只读）
主机 /srv/anchor-ci/workspace       -> 容器 /workspace（可写）
```

容器加入 `--add-host host.docker.internal:host-gateway`，使 Codex 可访问主机 broker。主机防火墙需允许容器网络访问 broker 的动态 TCP 端口，并限制其他网络的访问。Docker socket、主机状态和 Git 凭据不挂载进容器。

任务准备会将 `/workspace`、`/workspace/tasks` 和任务 ID 目录设为 root 所有的 `0711`，两类执行 UID 可以穿越到已知任务路径，但不能列出或替换其他任务。run 目录为 `0755`，agent 目录保持 UID 1001 所有的 `0700`；宿主生成的上下文文件在该私有目录内以 root 所有的 `0644` 原子写入，供 Codex 读取，测试 UID 不能进入该目录。测试源码、venv 和受限产物目录由既有 UID 1000/1001 规则管理。服务保留 `UMask=0077`，不依赖 Windows bind mount 的宽松权限。

## 镜像与版本环境

[Dockerfile.worker](Dockerfile.worker) 是生产受信 LLVM 配方。为每个版本设置已审核且固定 digest 的 `BASE_IMAGE`、已验证的 `CODEX_VERSION`、准确 LLVM revision 和构建资源限制。3.0 基础镜像还需真实 SDK、后端 checkout、设备访问及测试配置；没有这些条件时保留明确的未配置错误。将 SDK 的 `envsetup.sh` 等激活脚本放在 `tools.backend_env_scripts`，只由后端、算子及性能工具加载，缺少 SDK 不应阻塞独立前端检查。其他 Triton 版本只启用前端能力。

[worker.Dockerfile](worker.Dockerfile) 是无 LLVM/后端的轻量验收镜像，不能充当生产编译环境。两种镜像均使用测试 UID 1000、Codex UID 1001 和 root 所有的 `/opt/ci-venv`；seed 包含 build、setuptools、wheel、pybind11、pytest、jsonschema 和 PyYAML，任务 wheel 只安装到任务环境。

`tools.env` 不统一覆盖 `TRITON_BUILD_TYPE`，沿用被测项目的默认配置。当前项目默认 `TritonRelBuildWithAsserts`；普通 `Release` 和 `RelWithDebInfo` 的 `-DNDEBUG` 会与 3.0 的 LLVM debug 调用冲突。LLVM 配方自身的 `Release` 加 `LLVM_ENABLE_ASSERTIONS=ON` 是另一项设置，不应改成候选前端的编译参数。

将受信配方与 [prepare_llvm.sh](prepare_llvm.sh) 一并放到配置的 recipe context。首次接任务前准备镜像；后续 LLVM 选择从被测精确 Git 对象读取，变更只执行该受信配方。不要从候选 PR 下载并执行环境脚本。容器健康检查除工具可用性外，还独立验证 root 所有的 `/opt/llvm/anchor-ci-llvm-revision`。

主机 `codex.model` 设置 `deepseek-v4-flash`，`codex.reasoning_effort` 设置 `high`；`codex.provider` 使用示例中的官方地址和 `responses` 协议。将 `DEEPSEEK_API_KEY` 放在宿主 `service.env` 中，配置 JSON 只保存环境变量名称。宿主仅向 Codex 进程传递该密钥，不传给构建/测试工具；Codex 子命令排除该变量，并关闭可能恢复环境变量的登录 shell 和 shell 快照。

health 和 maintenance 的 systemd 模板通过 `UnsetEnvironment=DEEPSEEK_API_KEY` 排除模型密钥，仅 Poller 接收；若更改 `provider.env_key`，同步调整这两个模板中的变量名。

[模型目录](../deepseek-models.json) 按 [DeepSeek 官方 Codex 接入文档](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/) 声明上下文与工具能力，通过受信控制目录只读挂载。目录中的简短角色说明不替代 `ai_ci_program.md`。需要其他已验证的 Responses 服务时，由维护者修改 `provider` 和对应模型目录；不配置 `provider` 时仍支持 Codex 自身的认证方式及可选 `auth_file`。不要复制桌面插件、记忆或用户配置。生产控制目录必须与任务冻结的 `worker_revision_sha` 一致，更新控制目录前先排空任务。

## 启动服务

在主机仓库根目录完成配置和镜像准备后执行：

```sh
sudo python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json ensure
sudo python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-poller.service /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-health.service scripts/local_ci/deploy/anchor-ci-health.timer /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-maintenance.service scripts/local_ci/deploy/anchor-ci-maintenance.timer /etc/systemd/system/
sudo systemd-analyze verify --man=no /etc/systemd/system/anchor-ci-poller.service /etc/systemd/system/anchor-ci-health.service /etc/systemd/system/anchor-ci-health.timer /etc/systemd/system/anchor-ci-maintenance.service /etc/systemd/system/anchor-ci-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-ci-poller.service anchor-ci-health.timer anchor-ci-maintenance.timer
systemctl status anchor-ci-poller.service
journalctl -u anchor-ci-poller -u anchor-ci-maintenance
```

Poller 由 systemd 监督并在异常退出后重启；健康和维护使用独立 timer，不需要再增加任务编排服务。Windows 登录启动可用于本机持久验收，但不等于无需登录的 Linux 生产服务部署。

在现有持久 Linux worker 中，可用 root 执行 `LOCAL_CI_LINUX_PERMISSIONS_INTEGRATION=1 /usr/bin/python3 -I -S /opt/anchor-ci/tests/test_linux_permissions.py -v`。此测试在容器原生临时目录中验证 umask 0077、不同 UID、宿主证据读写与私有目录边界，不创建容器或修改真实任务。该验证不等于已经在另一台 Linux 服务器安装并启动 systemd 服务；真实服务器仍需按其路径、凭据和网络完成部署验收。

维护窗口使用 UTC，例如 `18:00` 对应北京时间次日 `02:00`。各版本设置不同 `stagger_minutes`，全局构建锁避免同时重建。窗口内先排空租约，检查磁盘，重建并替换同名 worker；新环境不健康则恢复旧容器。`rebuild --force` 只绕过窗口。不要手工删租约或回滚槽，也不要用全局 Docker 清理代替任务恢复。

## Gitee、Pages 与监控

主机 `relay.url/results_branch` 与 GitHub Actions 指向同一个明确的新 Gitee relay。`relay.username_env/token_env` 指定服务环境变量名；URL 不含凭据。发布只使用主机受信 Git 工作区，健康发布另用独立 checkout，避免与任务发布争用。

GitHub 配置 `GITEE_RESULTS_OWNER`、`GITEE_RESULTS_REPO`、`GITEE_RESULTS_REPO_URL`、`GITEE_RESULTS_WEB_URL`、`GITEE_RESULTS_BRANCH`、`GITEE_USERNAME` 和 secret `GITEE_TOKEN`；Pages 使用 `LOCAL_CI_PAGES_BRANCH=ci_repo`。同时设置外部贡献者审批 environment 的 Required reviewers 和必要检查，具体入口见 CI 指南。

健康服务发布结果分支中的 `health/<worker_id>.json`。先检查配置：

```sh
sudo python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
```

服务器 health 只采集并发布快照，不需要 SMTP、邮箱或 OAuth 配置。默认由 GitHub 独立读取快照：`main` 的现有 `ci-gateway.yml` 每 30 分钟转发到 `ci_repo` 的完整 [watchdog 工作流](../../../.github/workflows/local-ci-watchdog.yml)。维护者先在本仓库创建一个保持打开的运维 Issue，设置下表配置，手动验证后再开启调度。

| GitHub 配置 | 名称 |
| --- | --- |
| Variables | `LOCAL_CI_WATCHDOG_ENABLED=true`、`LOCAL_CI_HEARTBEAT_URL`、`LOCAL_CI_WORKER_ID`、`LOCAL_CI_OPERATIONS_ISSUE_NUMBER` |
| Variables | `LOCAL_CI_HEARTBEAT_MAX_AGE_SECONDS`，按定时调度延迟设置 |
| Secrets | 私有心跳源所需的 `LOCAL_CI_HEARTBEAT_READ_TOKEN`（可选） |

watchdog 只在故障变化或恢复时新增中文评论并更新 Issue 当前状态，相同状态不重复评论。订阅 Issue 的人按各自 GitHub Notifications／Email 设置接收，不能承诺指定邮箱一定收到邮件。服务器无需 GitHub 写令牌；GitHub forwarding job 和 watchdog job 仅为更新运维 Issue 使用 `issues: write`。

心跳 URL 必须返回原始 JSON。[watchdog.example.json](watchdog.example.json) 展示工作流生成的配置结构。通知写入只由 GitHub Actions 执行，使用其机器人评论作为跨运行的去重依据；不另配置宿主 watchdog 或个人发信令牌。
