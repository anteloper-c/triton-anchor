# Local CI 部署

本页说明服务安装；完整链路、工具选择和故障恢复见 [CI 指南](../../../docs/ci_guide_zh.md)，配置字段见 [config.example.json](../config.example.json)。模板存在不代表服务、真实后端或远端通知已经部署。

## 准备主机

生产服务使用持久 Linux 主机、Docker 和专用 `anchor-ci` 账户。该账户加入 Docker 组，管理以下目录；Docker 权限仅授予受信主机程序。

| 主机路径 | 所有者与用途 |
| --- | --- |
| `/opt/anchor-ci` | 维护者管理的精确 `ci_repo` checkout，服务只读 |
| `/etc/anchor-ci/config.json` | root 管理，服务账户可读；设置真实 profile 和 `local_acceptance=false` |
| `/etc/anchor-ci/service.env` | root 所有、`0600`；systemd 读取 Git/SMTP 凭据环境变量 |
| `/etc/anchor-ci/codex/auth.json` | 独立 Codex 认证，服务账户可读，其他用户不可读 |
| `/etc/anchor-ci/recipes/` | root 管理的镜像配方，不能指向 PR workspace |
| `/var/lib/anchor-ci` | 服务账户可写，配置为 `state_dir` |
| `/srv/anchor-ci/workspace` | 统一任务根目录，配置为 `workspace_host` |

各 profile 使用不同的固定容器名，共用以下挂载约定：

```text
主机 /opt/anchor-ci/scripts/local_ci -> 容器 /opt/anchor-ci（只读）
主机 /srv/anchor-ci/workspace       -> 容器 /workspace（可写）
```

容器加入 `--add-host host.docker.internal:host-gateway`，使 Codex 可访问主机 broker。主机防火墙需允许容器网络访问 broker 的动态 TCP 端口，并限制其他网络的访问。Docker socket、主机状态和 Git/SMTP 凭据不挂载进容器。

## 镜像与版本环境

[Dockerfile.worker](Dockerfile.worker) 是生产受信 LLVM 配方。为每个版本设置已审核且固定 digest 的 `BASE_IMAGE`、已验证的 `CODEX_VERSION`、准确 LLVM revision 和构建资源限制。3.0 基础镜像还需真实 SDK、后端 checkout、设备访问及测试配置；没有这些条件时保留明确的未配置错误。将 SDK 的 `envsetup.sh` 等激活脚本放在 `tools.backend_env_scripts`，只由后端、算子及性能工具加载，缺少 SDK 不应阻塞独立前端检查。其他 Triton 版本只启用前端能力。

[worker.Dockerfile](worker.Dockerfile) 是无 LLVM/后端的轻量验收镜像，不能充当生产编译环境。两种镜像均使用测试 UID 1000、Codex UID 1001 和 root 所有的 `/opt/ci-venv`；seed 包含 build、setuptools、wheel、pybind11、pytest、jsonschema 和 PyYAML，任务 wheel 只安装到任务环境。

将受信配方与 [prepare_llvm.sh](prepare_llvm.sh) 一并放到配置的 recipe context。首次接任务前准备镜像；后续 LLVM 选择从被测精确 Git 对象读取，变更只执行该受信配方。不要从候选 PR 下载并执行环境脚本。容器健康检查除工具可用性外，还独立验证 root 所有的 `/opt/llvm/anchor-ci-llvm-revision`。

主机 `codex.model` 设置 `gpt-5.5`，`codex.reasoning_effort` 设置 `high`；认证单独配置，不复制桌面插件、记忆或用户配置。生产控制目录必须与任务冻结的 `worker_revision_sha` 一致，更新控制目录前先排空任务。

## 启动服务

在主机仓库根目录完成配置和镜像准备后执行：

```sh
python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json ensure
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-poller.service /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-health.service scripts/local_ci/deploy/anchor-ci-health.timer /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-maintenance.service scripts/local_ci/deploy/anchor-ci-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-ci-poller.service anchor-ci-health.timer anchor-ci-maintenance.timer
systemctl status anchor-ci-poller.service
journalctl -u anchor-ci-poller -u anchor-ci-maintenance
```

Poller 由 systemd 监督并在异常退出后重启；健康和维护使用独立 timer，不需要再增加任务编排服务。Windows 登录启动可用于本机持久验收，但不等于无需登录的 Linux 生产服务部署。

维护窗口使用 UTC，例如 `18:00` 对应北京时间次日 `02:00`。各版本设置不同 `stagger_minutes`，全局构建锁避免同时重建。窗口内先排空租约，检查磁盘，重建并替换同名 worker；新环境不健康则恢复旧容器。`rebuild --force` 只绕过窗口。不要手工删租约或回滚槽，也不要用全局 Docker 清理代替任务恢复。

## Gitee、Pages 与监控

主机 `relay.url/results_branch` 与 GitHub Actions 指向同一个明确的新 Gitee relay。`relay.username_env/token_env` 指定服务环境变量名；URL 不含凭据。发布只使用主机受信 Git 工作区，健康发布另用独立 checkout，避免与任务发布争用。

GitHub 配置 `GITEE_RESULTS_OWNER`、`GITEE_RESULTS_REPO`、`GITEE_RESULTS_REPO_URL`、`GITEE_RESULTS_WEB_URL`、`GITEE_RESULTS_BRANCH`、`GITEE_USERNAME` 和 secret `GITEE_TOKEN`；Pages 使用 `LOCAL_CI_PAGES_BRANCH=ci_repo`。同时设置外部贡献者审批 environment 的 Required reviewers 和必要检查，具体入口见 CI 指南。

健康服务发布结果分支中的 `health/<worker_id>.json`。先检查配置：

```sh
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --publish
python3 -m scripts.local_ci.maintenance.watchdog --config /etc/anchor-ci/watchdog.json --dry-run
```

`smtp.account_emails` 必须填写 `heron-mc` 和 `likehupochuan` 对应的真实邮箱，另配 SMTP host、from 和凭据环境变量。缺少映射会报告 `notification_not_configured`，不会猜邮箱。`--dry-run` 禁止发信和 Git 发布；真实通知需验证实际收件，loopback SMTP sink 仅验证程序链路。

整机离线由另一主机运行 [watchdog 服务](anchor-ci-watchdog.service) 和 timer，或由 GitHub 定时入口监控。心跳 URL 必须返回原始 JSON，不能是 HTML 页面。`main` 仅保留定时/手动转发，完整 [watchdog 工作流](../../../.github/workflows/local-ci-watchdog.yml) 位于 `ci_repo`。

| GitHub 配置 | 名称 |
| --- | --- |
| Variables | `LOCAL_CI_WATCHDOG_ENABLED=true`、`LOCAL_CI_HEARTBEAT_URL`、`LOCAL_CI_WORKER_ID` |
| Variables | `LOCAL_CI_HEARTBEAT_MAX_AGE_SECONDS`，按定时调度延迟设置 |
| Variables | `LOCAL_CI_SMTP_HOST`、`LOCAL_CI_SMTP_PORT` |
| Secrets | `LOCAL_CI_SMTP_FROM`、`LOCAL_CI_SMTP_USERNAME`、`LOCAL_CI_SMTP_PASSWORD` |
| Secrets | `LOCAL_CI_EMAIL_HERON_MC`、`LOCAL_CI_EMAIL_LIKEHUPOCHUAN` |
| Secrets | 私有心跳源所需的 `LOCAL_CI_HEARTBEAT_READ_TOKEN`（可选） |

独立主机持久保存通知去重状态；GitHub workflow 使用缓存保存。仅在 Local CI 同机运行 watchdog 能发现服务故障，不能覆盖整机断电或断网。
