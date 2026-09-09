# jiwang_ci 服务器部署交接（仅 Gitee）

请在目标服务器上完成 Local CI 的部署准备与用户服务安装。服务器无法访问 GitHub，只能通过 Gitee 取得代码；本轮不配置 GitHub、不投递或试跑 PR、不进行线上联调、不调用模型生成、不发送邮件。安装后的 CI 服务和定时器保持未启用、未启动，避免自动消费已有队列；Rootless Docker 本身需要启动以完成部署预检。

唯一已确认的服务器信息是 CI 用户名 `jiwang_ci`。发行版、家目录、UID、资源额度、镜像、依赖和所有实际地址留待服务器盘点，不猜测。使用本目录的 [配置模板](config.template.json) 和 [凭据变量模板](credentials.env.template)，填写后的文件放在 Git checkout 外，不提交或打印凭据。

## 部署架构与边界

- 宿主机 `jiwang_ci` 以普通权限运行 Harness、Rootless Docker 和用户级 systemd 服务。该用户可以有人工维护用的 sudo 权限；自动 CI 服务使用 `NoNewPrivileges=yes`，不调用 sudo，也不加入系统 docker 组。
- 运行期每个 PR 任务使用独立容器，内含单一 Codex 和确定性工具。candidate/base/diagnostic/Codex 是容器内四个非 root UID，不需要新建四个宿主账号。镜像长期维护，任务容器不复用。
- 保持其他用户的服务、容器和数据不变；不停止系统 Docker，不执行全局 prune，不直接复用旧 CI 状态目录。若 `jiwang_ci` 下已有同名运行服务，先记录冲突，不覆盖或重启它。
- 本文是部署交接，不是运行期提示词。运行期唯一入口仍是 `scripts/local_ci/skills/local-ci/SKILL.md`。
- 服务器仅负责 Gitee 任务读取、执行和结果上传。GitHub 独立发布，不需要 Gitee receipt；本轮不处理任何 GitHub 侧配置。

## 1. 部署前需要拿到的材料

请先填写以下信息。缺失时列出缺项及需要谁提供，不访问 GitHub 补齐，不把占位内容当实际配置。

| 输入 | 当前值 / 获取方式 |
| --- | --- |
| CI 用户 | `jiwang_ci` |
| 服务器发行版、版本、家目录、UID | 留空；服务器盘点 |
| Gitee 控制代码仓库 URL | 留空；由交付方提供 |
| Gitee 控制代码 ref | 留空；交付方同步本次可信 `CI_dev` 后提供实际 ref |
| 预期控制代码完整 commit SHA | 留空；交付方随本次代码提供，服务器只做本地比对 |
| Gitee 任务/结果仓库、独立健康仓库和健康分支 | 留空；沿用实际中转配置，确认健康分支已存在 |
| CPU、内存、PID 和磁盘额度 | 留空；按 CI 可用额度配置，不按整机资源占满 |
| 基础镜像及不可变 digest、离线镜像文件或公司镜像源 | 留空；使用可信来源，不从执行过 PR 的容器制作镜像 |
| 各版本 LLVM、前端代码；3.0 的 PPL、仿真后端、torch/torch_tpu、FlagGems | 留空；提供精确版本、来源、路径及摘要 |
| Codex CLI、公司模型配置及私有凭据来源 | 留空；复用实际可用配置，不猜模型名或中转地址 |
| Gitee 凭据、SMTP、公司 CA/代理 | 留空；在服务器私下填写 |

**代码交付前提：** 能访问 GitHub 的交付方需要先将本次 `CI_dev` 控制代码同步到公司可达 Gitee，并给出 URL、ref、完整 SHA。这是交付方的工作，不要求服务器窗口连接 GitHub。已有 Gitee PR 代码 refs 不保证包含控制分支；只有 PR 快照时不能据此安装本版 Harness。`main` 的 GitHub 调度部署不在服务器窗口的工作范围。

服务器所需 Docker/Rootless 安装包、宿主 Python 及依赖、Codex CLI、镜像和 LLVM/PPL 等也必须已有本地文件、公司可达来源或 Gitee 镜像。不要默认公网包源可用，不执行从 GitHub 或其他不可达公网地址下载的安装脚本；缺材料就记录部署阻塞。

## 2. 建立账户和 Rootless Docker

先只读盘点 `getent passwd jiwang_ci`、`id jiwang_ci`、发行版/内核/systemd、磁盘文件系统和剩余空间，以及现有 Docker、旧 CI 服务和目录归属。

若账号不存在，由管理员按实际发行版创建，可授予人工维护用的 sudo。准备 uidmap/rootless 所需组件、用户 D-Bus/systemd、至少 65536 且与其他用户不冲突的 subuid/subgid、linger。实际安装命令取决于发行版和已有离线包，不在本材料中虚构。

通过真实 `jiwang_ci` 登录会话安装并启动该用户的 Rootless Docker service。与其他人的系统 Docker 共存，不停用系统 daemon。使用支持的本地文件系统存储 Docker 数据，不使用 NFS。

配置显式的该用户 endpoint/context，核对 Docker info 中的 rootless、cgroup v2 和 systemd；CPU/memory/pids controller delegation 仅按需要配置。后续资源预检会检查实际限制，不把传入 Docker 参数等同于限制生效。

## 3. 从 Gitee 取得并固定控制代码

使用交付方提供的 Gitee URL/ref 建立带完整提交历史的控制 checkout，仅获取指定控制 ref。目录归 `jiwang_ci` 所有；私有访问使用服务器上的凭据机制，不把 token 拼进命令或 URL。不需要下载 GitHub ZIP，也不需要服务器安装或调用 `gh`。

以下变量必须先填写实际值；`CI_CONTROL` 应为本次新目录。逐条执行，任一步失败即停止：

```bash
: "${CI_GITEE_CONTROL_URL:?填写 Gitee 控制仓库 URL}"
: "${CI_CONTROL_REF:?填写已同步到 Gitee 的控制 ref}"
: "${CI_EXPECTED_CONTROL_SHA:?填写交付方提供的完整 commit SHA}"
: "${CI_CONTROL:?填写控制 checkout 的绝对路径}"
test ! -e "$CI_CONTROL"
git init "$CI_CONTROL"
git -C "$CI_CONTROL" remote add origin "$CI_GITEE_CONTROL_URL"
git -C "$CI_CONTROL" fetch --no-tags origin "$CI_CONTROL_REF"
test "$(git -C "$CI_CONTROL" rev-parse 'FETCH_HEAD^{commit}')" = "$CI_EXPECTED_CONTROL_SHA"
git -C "$CI_CONTROL" checkout --detach "$CI_EXPECTED_CONTROL_SHA"
test -z "$(git -C "$CI_CONTROL" status --porcelain)"
```

SHA 比对依据是交付方提供的值，无须向 GitHub 查询。检查依赖仓库和子模块的实际源，不能让其继续从 GitHub 拉取。不要在控制 checkout 安装依赖或保存私有配置；镜像配方绑定控制提交，后续更换控制提交需重新准备匹配镜像和资源预检。

配置中的 `repositories: ["likehupochuan/triton-anchor"]` 是任务身份白名单，不是让服务器访问 GitHub 的 clone 地址，不能直接改成 Gitee URL。实际网络来源由 `gitee_repo_url`、各 profile 的 repositories/LLVM 配置等指定。不操作 `CI_dev_forPR`，不对 `RACE-org/triton-anchor` 执行远端操作。

## 4. 填写服务器配置与凭据

复制本目录两个模板到可信配置目录，归属 `jiwang_ci`，含凭据文件权限设为 600。所有空值须补齐；只配置本次已准备好的版本，其余版本明确记录为未部署。Triton 3.0 必须具备后端和性能能力，其他版本仅前端能力，不能把 3.0 缺失后端改为禁用以通过预检。

| 配置 | 填写规则 |
| --- | --- |
| `state_dir/control_root/codex_home` | 使用该用户所有的实际绝对路径；独立新 state、干净控制 checkout、公司专用 Codex 配置来源，与个人默认 `~/.codex` 分离。 |
| `worker_id` | 本次部署的唯一标识；由交付方提供或记录供其后续配置网关，本窗口不修改 GitHub。 |
| `python_bin/codex_bin` | 前者是宿主 Python，后者是镜像内 Codex CLI 的实际绝对路径；Dockerfile 不会自动联网安装 Codex。 |
| `runtime.endpoint/context`、`rpc_socket_dir` | 使用真实 `jiwang_ci` UID 的用户运行时目录；endpoint 类似 `unix:///run/user/实际UID/docker.sock`，context 必须指向同一 endpoint。 |
| `resources/max_jobs` | 真实 CPU、内存字节数、PID 上限；编译并行度默认 8，可按额度降低。 |
| `profiles` 的 key 与 `name` | key 必须等于后续任务的目标分支，由交付方确认；`name` 是镜像轮换命令的 profile 名，两者不可混用。模板中的目标分支需核实。 |
| `profile.image` | 已批准基础镜像的不可变 SHA256 引用。系统 Docker 的镜像不会自动出现在该用户的 Rootless daemon，须通过可信来源取得或校验后导入。 |
| `profile.repositories` | 以目录名为 key，填写公司可达 repository 和精确 commit；镜像中位于 `/opt/local-ci/runtime/目录名`，至少含自检用的 `triton-anchor`。 |
| `profile.archives` | 预编译依赖的 `archive/url`、`sha256`、`strip_components`；镜像中位于 `/opt/local-ci/runtime/deps/名称`。 |
| `profile.llvm` | source 使用可信 repository 和构建配置；archive 使用文件或可达 URL、sha256 和精确 commit。commit 必须匹配 `llvm_hash`，以对应源码的 `triton/cmake/llvm-hash.txt` 为准。 |
| `profile.env` | 实际 seed venv、后端 envsetup/测试命令、PPL/FlagGems 路径；`workspace_container` 默认 `/workspace` 是配方逻辑前缀，会改写为 `/opt/local-ci/runtime`，不是宿主挂载目录。 |
| `gitee_repo_url/health_repo_url/health_branch` | 实际中转地址和已有分支；记录任务/结果与健康发布所需权限。配置不代表已执行远端发布。 |

基础镜像需具备编译器、CMake/Ninja、Git、Python/venv 和依赖；seed Python 能 import build/setuptools/wheel/pybind11/yaml/pytest。3.0 还需要匹配的 PPL、torch/torch_tpu、仿真后端和 FlagGems；用户使用仿真，不预设物理设备，也不凭“已有 LLVM 和 PPL”判断全部依赖齐全。

保留公司实际 provider/model/config.toml/auth.json。它们位于宿主私有来源，运行时仅进入任务的 Codex 私有目录，不写入 Git、镜像层或普通测试环境。仅补充实际 provider 的 `env_key/env_http_headers` 所引用变量。SMTP 仅填写配置，不发送邮件。

`--credentials-env` 指定安装后服务读取的文件，不会替安装器当前进程加载变量。手动执行预检和安装前，在 `jiwang_ci` 会话中加载同时兼容 shell/systemd 格式的私有文件，不开启 xtrace：

```bash
: "${CI_CREDENTIALS:?填写私有 EnvironmentFile 的绝对路径}"
set -a
. "$CI_CREDENTIALS"
set +a
```

## 5. 准备镜像并完成部署预检

当前安装器要求可信镜像自检与资源限制实测通过。这些是部署前置，会构建镜像、运行受信代码的构建/import/后端自检和资源验证容器；不会领取 PR，也不运行 Codex 审查或调用模型生成。不能用模拟结果、`echo` 或 `true` 替代。

`validation_commands` 使用实际 argv：每个版本至少配置 `environment`、`frontend_build`、`wheel_install_import`、`frontend_smoke`，3.0 增加 `backend_rebuild`、`backend_smoke_jit`。命令格式如下，按检查 ID 替换最后一项，并使用镜像内实际 Python：

```json
["python3", "/opt/local-ci/control/scripts/local_ci/deploy/validate_environment.py", "environment"]
```

先填写 `CI_PYTHON`（实际宿主 Python）、`CI_CONFIG`（已填 JSON 的绝对路径）和 `CI_PROFILE_NAME`（本次 profile 的 name），保持凭据已加载：

```bash
: "${CI_CONTROL:?填写控制 checkout 的绝对路径}"
: "${CI_PYTHON:?填写宿主 Python 路径}"
: "${CI_CONFIG:?填写实际配置的绝对路径}"
: "${CI_PROFILE_NAME:?填写要准备的 profile name}"
cd "$CI_CONTROL"
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG" --configuration-only
"$CI_PYTHON" scripts/local_ci/deploy/rotate.py --config "$CI_CONFIG" --profile "$CI_PROFILE_NAME"
```

逐条执行，失败则停止。对配置中每个 profile 完成镜像准备后再执行：

```bash
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG" --probe-runtime
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG"
```

资源预检检查实际 CPU/memory/pids 限制和镜像身份。SMTP、公司模型配置或其他必需项缺失时如实记录，不能绕过正式预检安装，不能宣称部署完成。预检通过不代表真实模型、邮件、PR 或 GitHub 链路已验收。

## 6. 安装用户服务，保持未接单

确认 `jiwang_ci` 下没有同名活动 CI 服务/定时器，填写独立 `CI_REVIEW_DIR`，先渲染检查再安装：

```bash
: "${CI_REVIEW_DIR:?填写服务渲染目录的绝对路径}"
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR"
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR" --apply
```

安装器生成 Worker、health、retention 和已配置版本的镜像轮换用户服务/定时器，备份已有文件并执行用户级 daemon-reload；不会启动服务。检查生成文件中的运行路径、EnvironmentFile、Rootless endpoint、`NoNewPrivileges=yes` 和定时配置，记录安装器返回的备份目录。

本轮不启用或启动这些 CI unit，不运行 `worker.py --once`，不迁移生产队列、不停旧 CI。Worker 一旦启动会自动处理有效任务；health/retention 等定时器也会产生发布或清理操作，因此统一留到后续启用。本轮无需构造正常或失败 PR，也无需 GitHub Gateway 操作。

## 部署窗口的最终交付

只提交一份简洁部署记录：

- 实际 OS、用户/UID、目录及资源额度；Rootless daemon 状态和资源预检结果。
- Gitee 控制仓库/ref/完整 SHA，实际 worker_id 和配置文件路径（不含凭据内容）。
- 已准备的版本、镜像发布 ID/digest、LLVM/PPL/后端来源版本；未部署版本和缺失材料。
- 预检日志、已安装 unit 清单、安装备份位置；明确 CI 服务/定时器仍未启用接单。
- 若存在阻塞，列出已完成部分和缺项；明确未做 PR 试跑、真实模型调用、邮件发送及 GitHub 侧配置或验收。

上级 [部署 README](../README.md) 包含完整产品的运维、迁移和回滚参考；本轮执行范围以本文为准，不自动扩展到其中的上线与联调步骤。
