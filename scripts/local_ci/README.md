# Local CI：Codex 自主验证与受信证据

Local CI 使用每个 Triton 版本固定的常驻容器。GitHub 完成 PR 信息、Basic CI、API compatibility 和 Security Gate 检查，外部贡献者通过人工审批后，将精确被测提交与任务元数据投递到 Gitee。主机 Poller 校验任务及受信环境，Codex 理解变更、选择工具与补充用例，主机证据 broker 执行并记录命令。结果发布后，GitHub 再校验任务身份、时效和证据，更新检查、PR 评论与 Dashboard。

## 目录与职责

```text
scripts/local_ci/
├── ci.py                    # Poller 入口
├── ai_ci_program.md         # Codex 工作流程编排
├── config.example.json      # 主机受信配置示例
├── runtime/                 # 任务、broker、执行账本、结果与恢复
├── github/                  # GitHub 回写、审批卡和结果校验
├── schemas/                 # 结果与 AI 审查契约
├── tools/
│   ├── basic_tools/         # 构建、后端、算子及性能工具
│   ├── ai_review_tools/     # 架构契约与专项审查要求
│   └── ai_custom_tools/     # 当前任务的辅助工具边界
├── maintenance/             # 常驻容器、每日重建、健康与离线监控
├── deploy/                  # 受信镜像配方及 systemd 模板
└── tests/                   # 当前控制面测试
```

Local CI 控制面位于本目录及 `scripts/dashboard/`。`scripts/ci/` 和 `scripts/api_contract/` 保留既有 GitHub 基础检查及 API 检查能力；被测 PR 中的同名 Local CI 文件不会替代主机受信控制程序。

## 最低检查与工具

最低范围由主机策略计算，Codex 可以增加检查，不能减少必检项。工具依赖只约束真实构建产物，其余检查和审查的顺序由 Codex 根据影响自主决定。

| 变更 | 最低范围 |
| --- | --- |
| 纯文档 | 免构建，仍提交有源码证据的架构审查 |
| 编译器代码、打包或前端变更 | 环境、Frontend build、wheel 安装/import、Frontend smoke、架构审查 |
| 编译、lowering、pipeline、后端相关变更或影响不明 | 全部适用检查及架构审查 |
| CI 控制面变更 | 环境、控制面测试、架构审查；混合变更叠加编译器必检 |
| 手动 FlagGems 全量任务 | 全部适用检查，并执行全量算子清单 |

基础工具覆盖 `environment`、`frontend_build`、`wheel_install`、`frontend_smoke`、`backend_rebuild`、`backend_smoke`、`flaggems`、`compile_time`、`pass_profile`、`ir_serialization`。只有 Triton 3.0 具有后端、算子及性能能力；其他版本的后六项明确为 `not_applicable`。3.0 缺少后端配置或依赖会失败，不能标为不适用。

FlagGems 默认按影响选测；未给出可确定的算子集合时执行历史支持清单。显式受影响的算子即使不在历史通过清单中也必须测试。全量模式仅由手动触发元数据开启。性能命令失败阻塞；耗时变化展示为诊断，不单独阻塞合入。没有匹配的可信基线时报告 `baseline_available=false`，不声称已证明无性能回退。参数和工具 API 见 [tools/README.md](tools/README.md)。

性能基线优先使用主机 `profile.performance_baselines` 中显式配置的精确提交、环境和哈希记录；其次自动复用本机 `state_dir/runs/` 中已成功发布、基准 SHA 与 profile/LLVM 匹配的运行。对应工具须有成功 receipt，候选 JSON 的主机冻结哈希必须吻合；复制到当前任务后由主机设为只读。历史缺失、配置不匹配或文件损坏时不推测基线数值。

Codex 在常驻容器内调用主机 broker：

```sh
python3 /opt/anchor-ci/runtime/client.py status
python3 /opt/anchor-ci/runtime/client.py environment
python3 /opt/anchor-ci/runtime/client.py frontend_build --parameters '{"jobs":2}'
```

`finalize` 提交符合 schema 的审查，主机将其与真实 receipt、日志哈希、产物哈希及最低范围合并。终端文本、AI 自报成功或人工直接执行工具的输出都不能替代受信 receipt。

## 主机配置与持久部署

从 [config.example.json](config.example.json) 生成主机 `/etc/anchor-ci/config.json`。镜像、依赖目录、分支、后端 JIT 命令、邮箱及认证路径必须替换成真实配置；示例本身不表示已部署。

* `profiles` 定义版本、精确 LLVM revision、固定容器名、工具参数和维护配方；`branch_profiles` 将实际目标分支映射到 profile。配置由维护者管理，不读取 PR 提供的环境配方。
* 主机 `/opt/anchor-ci` 是受信仓库 checkout。容器只读挂载 **主机的 `/opt/anchor-ci/scripts/local_ci` 到容器 `/opt/anchor-ci`**，因此 launcher 和工具分别位于容器 `/opt/anchor-ci/runtime` 和 `/opt/anchor-ci/tools`。
* `workspace_host` 是主机统一任务目录，所有 profile 将它挂载到 `/workspace`。任务路径为 `/workspace/tasks/<task_id>/<run_id>/`，含 source、artifacts、agent 和独立 venv；每个版本仍使用各自固定的容器。
* `state_dir` 保存主机账本、租约、不可变结果和待发布状态。它与 Docker socket、GitHub/Gitee 凭据均不挂载给被测代码。
* `seed_venv` 默认为 root 所有的 `/opt/ci-venv`。测试 UID 1000 使用其任务副本，Codex UID 1001 使用独立认证目录；两者不能修改受信工具或 seed。候选 wheel 安装不改写共享 seed。
  Python 构建、测试及其子进程通过只读 `runtime/task_python` 入口，跳过任务 `.pth` 与 `sitecustomize`；受信预检和产物清单另用系统 Python `-I -S`，避免任务环境启动代码伪造成功。
* `dependency_sources` 只能为 `triton`、`FlagGems` 指定已准备的受信本地 Git 源。控制器按被测提交的精确 gitlink 检出，不递归执行候选 `.gitmodules` 的网络地址。
  仅为目标分支的实际 gitlink 配置该字段；仓库已跟踪的普通目录不能覆盖。当前示例只为 3.0 的 FlagGems gitlink 提供来源。
* LLVM 选择读取被测提交中的 `triton/cmake/llvm-hash.txt`；若 Triton 是 gitlink，则读取该精确对象中的 hash。首次部署可通过 `dependency_host_sources.triton` 提供主机受信镜像；否则需要已运行的固定容器和 `dependency_sources.triton`。不会使用依赖镜像的 HEAD 代替 gitlink，也不会执行候选仓库的 LLVM 配方。

生产执行必须证明实际控制面与任务冻结的 `worker_revision_sha` 一致。Git checkout 必须匹配 HEAD，完整控制目录不得存在未提交、额外或缺失文件；无 Git 的部署副本需要主机显式指定 `control_manifest`，记录受信 revision 和完整 SHA-256 文件清单。`local_acceptance=true` 只供本机开发验收，结果身份为未验证，不能通过 GitHub 生产门禁。

从主机仓库根目录执行：

```sh
python3 scripts/local_ci/ci.py poll --config /etc/anchor-ci/config.json --once
python3 scripts/local_ci/ci.py poll --config /etc/anchor-ci/config.json
python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json ensure
python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json inspect
python3 -m scripts.local_ci.maintenance --config /etc/anchor-ci/config.json rebuild
python3 -m scripts.local_ci.maintenance.health --config /etc/anchor-ci/config.json --dry-run
```

`ensure` 启动或检查固定容器，Poller 接任务前也会调用它。首次启动前需按受信配方准备镜像。每日维护在 UTC 窗口内先排空任务，再重建并替换同名常驻容器，失败恢复旧容器。`--force` 仅绕过维护窗口，仍遵守租约和磁盘检查。任务之间不会创建一次性容器。镜像、服务安装、健康发布和独立 watchdog 的操作见 [deploy/README.md](deploy/README.md)。

## 认证、通知与恢复

`codex.auth_file` 选择主机的独立 `auth.json`；控制器只复制该文件，使用 `--ignore-user-config`，不复制桌面配置、插件或记忆。不要将 API key 写入任务、profile 工具环境或仓库。主机发布服务持有 Gitee 凭据，GitHub Actions 持有 GitHub 回写权限，Codex 仅持有当前任务 broker 令牌；被测构建命令不接收这些令牌。`relay.username_env/token_env` 指定主机环境变量名，URL 中不得含凭据。

健康服务独立于 Poller，检查容器、租约、任务心跳、磁盘和 systemd。`health.publish=true` 或 health CLI 的 `--publish` 将快照发布到结果分支 `health/<worker_id>.json`，使用独立 checkout。`--dry-run` 同时禁止邮件和发布。SMTP 收件人由 `smtp.account_emails` 显式映射指定 Gitee 用户；缺失时产生 `notification_not_configured`，不会猜测邮箱。另一主机或 GitHub 定时 watchdog 读取该 HTTPS JSON，发现服务或整机离线；只部署在同机不能覆盖整机掉电。邮件故障及恢复通知有持久去重与失败重试。

| 持久状态 | 恢复行为 |
| --- | --- |
| `preparing` / `running` | Poller 获得全局锁后确认租约、清理固定测试/agent UID 的残留进程，以同一 run_id 继续任务并加载有效 receipt |
| `publish_pending` | 只重试已保存的不可变结果；失败记录尝试次数和诊断，不重复构建 |
| 发布诊断 | 如保存了原 Codex session，允许有界恢复该会话分析发布原因；不给构建工具或远端凭据，仍由主机重试发布 |
| `published` | 保存完成记录，清除运行中健康状态，同一任务不会重复执行 |
| 无法确认进程已清理 | 保留租约和错误，阻止并行任务误用环境 |

账本在 `state_dir/runs/<task_id>/<run_id>/`，关键文件为 `execution.json`、`task.json`、`result.json`、`report.md`、receipt 和日志。结果分支的 `runs/<task_id>/<run_id>/` 存放不可变运行，`tasks/<task_id>/latest.json` 指向最新结果。任务提交、审批身份、取消时间和产物哈希失配均不能回写成功。长期规则、基础工具和架构契约修改必须交维护者采纳；AI 自修复仅限当前任务。

## GitHub 接入与验证

当前 router/worker 合约为 v4，任务元数据为 v2，结果为 v4。先部署匹配的受信 worker，再更新 main 的最小 router；main 将任务委派给 `ci_repo`。外部 PR 在前置检查成功后进入 `local-ci-fork-approval` environment。目标分支保护规则必须要求 `local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`；工作流存在不等于保护规则已配置。

```sh
python3 -m pytest -q scripts/local_ci/tests scripts/ci/tests
```

本机测试包含真实本地 bare Git 投递/发布、失败重试、身份与证据负向检查、工具计划和性能解析验证。真实 wheel 测试可用 `CI_TOOLS_TEST_PYTHON` 指定可写的独立测试解释器开启。合成 fixture 的构建及轻量容器链路通过，不等于 Triton/LLVM 编译、3.0 后端硬件、算子或性能验收，也不代表邮件和远端保护规则已部署。扩展与测试边界见 [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)。
