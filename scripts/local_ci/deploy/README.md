# Local CI 常驻服务部署

本目录是部署模板和受信环境配方示例。复制模板、配置真实环境并完成验证才构成部署；仓库中的示例不会自动创建服务、发送邮件或配置远端。

## 服务与目录

- `/opt/anchor-ci`：主机上维护者审核的完整仓库 checkout，Poller 从此目录启动。仅将其 `scripts/local_ci` 子目录只读挂载到容器 `/opt/anchor-ci`，使 launcher 位于容器 `/opt/anchor-ci/runtime/container_process.py`。
- `/etc/anchor-ci/config.json`：root 所有的服务配置，禁止 PR 写入。profile `id`、固定容器名称、LLVM revision、命令与重建配方均在此配置。
- `/etc/anchor-ci/service.env`：权限 `0600` 的认证环境变量。Docker socket、GitHub/Gitee 凭据均不挂载到被测容器。
- `/var/lib/anchor-ci`：服务状态、租约、维护日志和待发布产物；配置 `state_dir` 指向此目录。
- `/srv/anchor-ci/workspace`：与顶层 `config.workspace_host` 一致的可写 workspace，各版本固定容器均挂载到 `/workspace`。Engine 在其下按 task/run 隔离目录；每版本仍复用自己的容器。控制面与配方不能位于被测 workspace 中。
- `/etc/anchor-ci/recipes/<version>`：root 所有的可信 Docker 构建上下文，可由本目录参考配方派生；不得直接指向 PR checkout。

主机使用专用 `anchor-ci` 服务账户及 Docker 组。`docker` 组本身具备主机级权限，所以仅主机受信 Poller/维护服务持有，容器内 Codex 不拥有该组的主机 socket。

从仓库根目录验证并安装服务（需根据实际磁盘路径和账号完成配置）：

```sh
python3 scripts/local_ci/ci.py --help
python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json ensure
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-poller.service /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-health.service scripts/local_ci/deploy/anchor-ci-health.timer /etc/systemd/system/
sudo install -m 0644 scripts/local_ci/deploy/anchor-ci-maintenance.service scripts/local_ci/deploy/anchor-ci-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-ci-poller.service anchor-ci-health.timer anchor-ci-maintenance.timer
```

常用检查：`systemctl status anchor-ci-poller`、`journalctl -u anchor-ci-maintenance` 和 `state_dir/health/latest.json`。健康服务独立于 Poller，即使 Poller 崩溃仍可发现本机故障；整机断线需要下文的外部 watchdog。

## Profile 生命周期字段

下例只展示生命周期部分，完整 profile 还需配置工具及能力。3.0 profile 才能声明后端、FlagGems 与性能能力；其他版本只声明实际可用的前端能力。

```json
{
  "id": "triton-3.0",
  "triton_version": "3.0",
  "seed_venv": "/opt/ci-venv",
  "llvm_revision": "REPLACE_WITH_EXACT_40_HEX_REVISION",
  "container": {
    "name": "anchor-ci-triton-3-0",
    "image": "anchor-ci/triton-3.0:maintained",
    "run_args": [
      "--add-host", "host.docker.internal:host-gateway",
      "--mount", "type=bind,src=/opt/anchor-ci/scripts/local_ci,dst=/opt/anchor-ci,readonly",
      "--mount", "type=bind,src=/srv/anchor-ci/workspace,dst=/workspace"
    ],
    "healthcheck": ["sh", "-c", "test -x /opt/llvm/bin/mlir-opt && python3 --version && codex --version"]
  },
  "maintenance": {
    "window_start": "18:00",
    "window_minutes": 120,
    "stagger_minutes": 0,
    "disk_path": "/var/lib/docker",
    "min_free_gb": 40,
    "build_timeout_seconds": 14400,
    "recipe": {
      "context": "/etc/anchor-ci/recipes/triton-3.0",
      "dockerfile": "Dockerfile.worker",
      "build_args": {
        "BASE_IMAGE": "REPLACE_WITH_TRUSTED_BACKEND_IMAGE_AT_SHA256_DIGEST",
        "CODEX_VERSION": "REPLACE_WITH_VERIFIED_VERSION",
        "LLVM_BUILD_JOBS": "4"
      }
    }
  }
}
```

所有维护窗口使用 UTC。`18:00` 对应北京时间次日 `02:00`；给不同版本设置不同 `stagger_minutes`。全局构建锁进一步避免不同版本同时重建。每天成功重建一次；重建失败会记录故障，在窗口内下次 timer 执行时重试。

维护先标记 draining，已有任务继续完成，后续任务暂停获取该 worker。无任务租约后检查空间、构建受信镜像、停止旧 worker、临时改名保留回滚槽，以原固定名称启动替换 worker 并执行健康检查。检查失败则恢复原 worker。此替换发生在维护期，PR 之间持续复用容器，不会每个任务创建一次性容器。窗口结束尚未开始重建时解除 draining，下一窗口再尝试。

`rebuild --force` 可绕过窗口，但不能绕过在运行的任务租约或磁盘检查。主机从本次精确 Git revision 的 `triton/cmake/llvm-hash.txt`，或 frozen Triton gitlink 对应的受信依赖 Git 对象，读取所需 LLVM revision；不会执行 PR 提供的重建脚本。需要变化时使用主机配置的受信配方重建固定 worker，成功后才能获取任务租约。首次启动使用 gitlink 时，应配置受信 host 依赖镜像目录以读取 Git 对象；缺少所需对象或配方会明确阻塞。

成功选择保存在 `state_dir/workers/<profile-id>.selection.json`，每日维护和 `ensure` 自动沿用。该记录绑定完整受信 profile 的配置摘要；维护者更新 profile 后旧选择失效，重新根据配置和任务 revision 选择。记录只能改变 LLVM revision 与其来源证据，不能替换容器、基础工具或配方。`prepare_llvm.sh` 从官方 LLVM 仓库获取并验证精确 revision，编译安装并保存 `/opt/llvm/anchor-ci-llvm-revision`。WorkerManager 使用 root 所有的 `/usr/bin/python3 -I` 独立核验该标记的内容、所有者及写权限；profile healthcheck 另检验实际 MLIR 工具，不要在健康命令中硬编码初始 LLVM hash。后端 base image、依赖源及受信配方仍由维护者配置。

任务租约位于 `state_dir/workers/<profile-id>.json`。进程异常退出后不根据超时擅自清空租约，避免另一个任务与孤儿编译并行。恢复时先确认或终止旧任务全部容器进程，再通过 Poller 的恢复流程释放该任务租约。维护事务同时持久记录旧容器回滚槽；重启后的维护恢复旧 worker 后重试。不要手工删除回滚槽或全局执行 `docker system prune`。

测试代码用 UID 1000、Codex 用 UID 1001，两者的基础解释器必须由 root 所有且不可写。容器 launcher 使用 `/usr/bin/python3 -I /opt/anchor-ci/runtime/container_process.py`，为每个 UID 保留独立的 `0700` PID 目录。单个命令停止先 TERM，等待后 KILL，并校验进程开始时间；任务结束时由受信 host 以 root 调用 `clean-users`，对固定 UID 1000/1001 清理包括 `setsid()` 脱离进程组的残留。该操作不接受动态 UID；仅在该固定 worker 的任务结束/恢复期间使用。未确认清理完成时不得释放任务租约。

## 邮件与外部离线监控

用户指定通知 Gitee 用户 `heron-mc` 和 `likehupochuan`。Gitee 用户名无法作为邮箱使用，配置必须显式填写 `smtp.account_emails`、`host`、`from`，SMTP 用户名与口令通过环境变量提供。缺少映射时健康快照包含 `notification_not_configured`，不会猜测地址或悄悄忽略。相同故障去重；发送失败保留待重试状态，故障恢复发送恢复通知。

健康服务通过 `maintenance.publish_health` 将 `state_dir/health/latest.json` 发布到同一 `config.relay.url`、`config.relay.results_branch` 下的 `health/<worker_id>.json`。它使用独立的 `state_dir/health-publication/repo`，不会与任务发布器共享 Git 工作区；push 遇到并发更新时最多重试三次并 rebase，冲突/失败保留快照，下次健康 timer 重试。`health.publish: true` 或 CLI `--publish` 开启发布，systemd 模板已接入。`--dry-run` 同时禁止邮件和 Git 发布。

```sh
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --publish
python3 -m scripts.local_ci.maintenance.publish_health --config /etc/anchor-ci/config.json
```

`relay.username_env` 和 `relay.token_env` 指向服务环境变量。健康与任务 Git 传输共用 host-only 配置：凭据只传入子进程环境，不写进 argv、Git 配置文件或错误日志；禁用继承的 Git trace、目录重定向与 credential helper。配置 URL 必须是显式 HTTPS 且不含凭据；本机验收也支持绝对本地 bare Git 路径。

`watchdog.example.json` 的 URL 应指向可返回上述 JSON 的独立 HTTPS 原始文件/API 端点，不能填返回 HTML 的 blob 页面。私有地址可以配置读取令牌的环境变量；URL 中不要嵌入凭据。

在另一主机安装 `anchor-ci-watchdog.service` 与 timer，或由 GitHub 的定时 workflow 运行：

```sh
python3 -m scripts.local_ci.maintenance.watchdog --config /etc/anchor-ci/watchdog.json --dry-run
```

验证配置与收件人后去掉 `--dry-run` 才会发送邮件。watchdog 以退出码 1 表示有故障，0 表示健康；GitHub 可据此保留失败 run。GitHub 定时运行可能延迟，应按实际调度周期设置 `max_age_seconds`。通知去重文件要放在独立主机持久目录，或由 workflow 持久保存，不能依靠每次消失的 runner 工作目录。只在同一服务器运行 watchdog 可以发现服务离线，无法覆盖整机掉电或网络断开。

`.github/workflows/local-ci-watchdog.yml` 提供独立 GitHub runner 监控，默认不启用定时告警。部署到默认分支后，配置以下值才能启用：

| 配置位置 | 名称 | 用途 |
| --- | --- | --- |
| Variables | `LOCAL_CI_WATCHDOG_ENABLED=true` | 开启每 15 分钟的外部检查 |
| Variables | `LOCAL_CI_CONTROL_BRANCH` | 受信控制分支，默认 `ci_repo` |
| Variables | `LOCAL_CI_HEARTBEAT_URL`、`LOCAL_CI_WORKER_ID` | 明确的原始心跳 URL 与 worker 身份，无旧仓库默认值 |
| Variables | `LOCAL_CI_HEARTBEAT_MAX_AGE_SECONDS` | 心跳过期阈值，默认 1800 秒，需考虑 GitHub 调度延迟 |
| Variables | `LOCAL_CI_SMTP_HOST`、`LOCAL_CI_SMTP_PORT` | TLS SMTP 服务与端口 |
| Secrets | `LOCAL_CI_SMTP_FROM`、`LOCAL_CI_SMTP_USERNAME`、`LOCAL_CI_SMTP_PASSWORD` | 发信身份与认证 |
| Secrets | `LOCAL_CI_EMAIL_HERON_MC`、`LOCAL_CI_EMAIL_LIKEHUPOCHUAN` | 两个 Gitee 账户对应的实际通知邮箱 |
| Secrets | `LOCAL_CI_HEARTBEAT_READ_TOKEN` | 私有心跳源读取令牌，可选 |

workflow 只允许目标个人 fork 运行，缺少必要配置时显式失败；通过 Actions cache 保留通知去重状态，并归档 watchdog 结果。配置文件中的邮箱不会被上传为 artifact。该模板存在不代表 GitHub 定时运行或真实邮箱通知已经启用。

## 本机验证边界

`python3 -m unittest scripts.local_ci.tests.test_maintenance_workers scripts.local_ci.tests.test_maintenance_health` 使用可控 Docker 命令适配器和真实跨进程文件锁，验证复用、排空、失败回滚、恢复、磁盘与离线故障、通知重试。它不代替真实 worker 的构建、LLVM、后端或性能测试，也不代表邮件和外部 watchdog 已部署。

`worker.Dockerfile` 是轻量本机验收镜像（Codex 固定 0.153.4，双 UID，无 LLVM/后端）；`Dockerfile.worker` 是生产环境可信 LLVM 配方参考。前者可直接 `docker build -f scripts/local_ci/deploy/worker.Dockerfile -t anchor-ci:acceptance .`，同样仅启动并复用一个常驻容器。在独占且空闲的验收 worker 中，以 root 和环境变量 `LOCAL_CI_PROCESS_CLEANUP_TEST=1` 运行 `tests/test_maintenance_processes.py`，可验证双 UID 运行、忽略 SIGTERM 的进程、后台子进程、`setsid()` 残留及清理权限。不要在通用宿主机设置该测试变量。

两个镜像均提供 root 所有的 `/opt/ci-venv`，预装 build、setuptools、wheel、pybind11、pytest、jsonschema 与 PyYAML；Codex 安装由 root 拥有。Engine 将 seed 复制到任务独立 venv 后赋予 build 用户写权限，Agent 与可信 launcher 始终使用 root 所有的解释器。

`test_maintenance_delivery.py` 使用真实本地 bare Git 和 `127.0.0.1` SMTP sink，验证“健康快照 → Git 发布 → watchdog 判断 → 邮件接收”的故障、重试、去重和恢复过程，收件地址为运行时生成的 `.invalid` 合成地址，仅进入 loopback sink。该验证不连接实际 SMTP 服务，不等同于真实账户收件验收。
