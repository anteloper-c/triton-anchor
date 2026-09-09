# jiwang_ci 服务器部署与调试手册（仅 Gitee）

请在目标服务器上完成 Local CI 的部署准备与用户服务安装。服务器无法访问 GitHub，只能通过 Gitee 取得代码；本轮不配置 GitHub、不投递或试跑 PR、不进行线上联调、不调用模型生成、不发送邮件。安装后的 CI 服务和定时器保持未启用、未启动，避免自动消费已有队列；Rootless Docker 本身需要启动以完成部署预检。

唯一已确认的服务器信息是 CI 用户名 `jiwang_ci`。发行版、家目录、UID、资源额度、镜像、依赖和所有实际地址留待服务器盘点，不猜测。使用本目录的 [配置模板](config.template.json) 和 [凭据变量模板](credentials.env.template)，填写后的文件放在 Git checkout 外，不提交或打印凭据。

本文给服务器部署窗口逐步执行；整体架构见 [实现说明](../../../../docs/ci_v4_implementation.md)，通用迁移与回滚见 [部署 README](../README.md)。聊天压缩交接放在开发机仓库外的 `方案/CI_对话交接.md`，不会随仓库推送，需由用户另行提供；服务器安装所需步骤和模板均在仓库内。

依赖依据是仓库的 [docs/build.md](../../../../docs/build.md)，实际环境变量核对 [envsetup.sh](../../../../envsetup.sh)。用户会自行下载、放置预编译包，并可以只向部署窗口提供文件路径；本手册优先采用本地 LLVM/PPL 等包，不要求服务器在线下载或从源码编译 LLVM。部署窗口负责检查文件、计算 SHA256、确认包版本并补齐配置字段。

## 先理解要部署什么

部署时先准备可以重复使用的镜像，再安装负责接任务的宿主服务。镜像准备和 PR 执行是两个阶段：

```text
部署阶段
jiwang_ci + Rootless Docker
  → Gitee 上的可信 CI_dev 控制代码
  → 基础镜像 + 本地预编译包 + 各版本源码与配置
  → rotate.py 构建并自检 CI 镜像
  → preflight.py 验证配置与资源限制
  → install.py 安装用户服务，暂不接单

后续运行阶段（本轮不启动）
宿主 Harness 读取 Gitee 任务
  → 从对应 CI 镜像创建独立任务容器
  → 容器内 Codex 原生分析与实验，经 MCP/Harness 执行正式检查
  → 宿主保存状态和证据、上传 Gitee
  → 按保留策略清理该任务的容器和数据
```

`profile.image` 填的是作为构建起点的**基础镜像**。`rotate.py` 加入可信控制代码、版本源码、LLVM/PPL 等依赖，生成带新 digest 的**CI 镜像**，验证后登记为活动版本。PR 使用后者；不需要预先启动三个常驻测试容器。多个版本可以共用合适的基础镜像，但各自的 LLVM 和后端能力由 profile 区分。

下面的命令都在服务器 Bash 中逐条执行，前一步失败就停在该步。除第 2 步明确的管理员准备外，使用 `jiwang_ci`，不加 sudo。检查结果和日志保留在自己的目录，便于之后从失败阶段继续。

## 部署架构与边界

- 宿主机 `jiwang_ci` 以普通权限运行 Harness、Rootless Docker 和用户级 systemd 服务。该用户可以有人工维护用的 sudo 权限；自动 CI 服务使用 `NoNewPrivileges=yes`，不调用 sudo，也不加入系统 docker 组。
- 运行期每个 PR 任务使用独立容器，内含单一 Codex 和确定性工具。candidate/base/diagnostic/Codex 是容器内四个非 root UID，不需要新建四个宿主账号。镜像长期维护，任务容器不复用。
- Codex 使用 `danger-full-access`、`approval_policy=never`，启用原生 Shell、unified exec 和编辑。原生实验在 `/codex/workspace/candidate/` 的独立源码、venv 和缓存中进行；源码来源清单绑定冻结提交，副本不包含可变 Git 元数据。正式检查及阻断复现仍走 MCP/Harness。
- 四身份分别保护候选安装、基线安装、只读诊断和 Codex 会话；不增加四份常驻进程，也不需要手工维护四个账号。原生命令与 Codex 同身份，能读取其模型认证和当前任务 RPC；这部分没有测试身份的凭据隔离。
- Codex 非 root，任务根文件系统只读。原生 venv 可安装实验依赖，apt、系统库和全局驱动应在可信镜像配方中准备。原生命令事件与源码快照保存在宿主私有任务记录，不直接作为正式通过证据或自动上传 Gitee。
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
| 各版本 LLVM、前端代码；3.0 的 PPL、仿真后端、torch/torch_tpu、FlagGems | 用户放好包并提供路径；部署窗口计算摘要、核对实际版本/来源，源码通过 Gitee 取得 |
| Codex CLI、公司模型配置及私有凭据来源 | 留空；复用实际可用配置，不猜模型名或中转地址 |
| Gitee 凭据、公司 CA/代理 | 留空；在服务器私下填写；此部署不配置 SMTP |

**代码交付前提：** 能访问 GitHub 的交付方需要先将本次 `CI_dev` 控制代码同步到公司可达 Gitee，并给出 URL、ref、完整 SHA。这是交付方的工作，不要求服务器窗口连接 GitHub。已有 Gitee PR 代码 refs 不保证包含控制分支；只有 PR 快照时不能据此安装本版 Harness。`main` 的 GitHub 调度部署不在服务器窗口的工作范围。

服务器所需 Docker/Rootless 安装包、宿主 Python 及依赖、Codex CLI、镜像和 LLVM/PPL 等也必须已有本地文件、公司可达来源或 Gitee 镜像。不要默认公网包源可用，不执行从 GitHub 或其他不可达公网地址下载的安装脚本；缺材料就记录部署阻塞。

## 2. 建立账户和 Rootless Docker

先只读盘点 `getent passwd jiwang_ci`、`id jiwang_ci`、发行版/内核/systemd、磁盘文件系统和剩余空间，以及现有 Docker、旧 CI 服务和目录归属。

若账号不存在，由管理员按实际发行版创建，可授予人工维护用的 sudo。准备 uidmap/rootless 所需组件、用户 D-Bus/systemd、至少 65536 且与其他用户不冲突的 subuid/subgid、linger。实际安装命令取决于发行版和已有离线包，不在本材料中虚构。

通过真实 `jiwang_ci` 登录会话安装并启动该用户的 Rootless Docker service。与其他人的系统 Docker 共存，不停用系统 daemon。使用支持的本地文件系统存储 Docker 数据，不使用 NFS。

配置显式的该用户 endpoint/context，核对 Docker info 中的 rootless、cgroup v2 和 systemd；CPU/memory/pids controller delegation 仅按需要配置。后续资源预检会检查实际限制，不把传入 Docker 参数等同于限制生效。

**检查点：** 登录 `jiwang_ci` 后，先确认身份、用户服务总线和私有 Docker，再继续准备目录：

```bash
id
test "$(id -un)" = jiwang_ci
systemctl --user status docker.service --no-pager
CI_DOCKER_ENDPOINT="unix:///run/user/$(id -u)/docker.sock"
test -S "/run/user/$(id -u)/docker.sock"
docker --host "$CI_DOCKER_ENDPOINT" info --format '{{json .SecurityOptions}}'
docker --host "$CI_DOCKER_ENDPOINT" info --format '{{.CgroupVersion}} {{.CgroupDriver}}'
docker context ls
```

预期有 `rootless`、`2 systemd`，context 指向相同 socket。`docker.service` 若在实际部署中另有名称，要同步修改配置和命令。无法连接用户总线时先修复真实登录会话/D-Bus/linger；socket 不存在时查 Rootless 安装和 `journalctl --user -u docker.service -n 100 --no-pager`，不要用 sudo Docker 绕过。

下面提供一套**建议目录**，使用该用户实际 `$HOME` 推导，不假定家目录是 `/home/jiwang_ci`。如果磁盘另有规划，可在创建前改 `CI_ROOT`：

```bash
CI_ROOT="$HOME/triton-anchor-ci"
CI_CONTROL="$CI_ROOT/control"
CI_STATE="$CI_ROOT/state"
CI_CONFIG="$CI_ROOT/config/local-ci.json"
CI_CREDENTIALS="$CI_ROOT/config/credentials.env"
CI_MODEL_SOURCE="$CI_ROOT/config/codex-source"
CI_REVIEW_DIR="$CI_ROOT/deploy-review"
CI_LOG_DIR="$CI_ROOT/deploy-logs"
umask 077
mkdir -p "$CI_ROOT/config" "$CI_MODEL_SOURCE" "$CI_STATE" "$CI_LOG_DIR" "$CI_ROOT/packages"
```

`control` 只存 Git 代码，`config` 存私有配置，`state` 存任务与镜像登记，`packages` 可存用户准备的离线包，`deploy-logs` 存部署命令输出。Docker 镜像/卷实际位于该用户 Rootless daemon 的 data-root，通过 `docker info` 查看。重新开终端后要重新设置这些变量；不要覆盖已有部署的配置或 state。

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

**检查点：** HEAD 等于交付 SHA、工作区干净，且 `scripts/local_ci/deploy/install.py` 存在。然后准备宿主 Harness 的独立 Python 环境，并复制配置模板：

```bash
python3 --version
python3 -m venv "$CI_ROOT/harness-venv"
CI_PYTHON="$CI_ROOT/harness-venv/bin/python"
test ! -e "$CI_CONFIG"
test ! -e "$CI_CREDENTIALS"
cp "$CI_CONTROL/scripts/local_ci/deploy/jiwang_ci/config.template.json" "$CI_CONFIG"
cp "$CI_CONTROL/scripts/local_ci/deploy/jiwang_ci/credentials.env.template" "$CI_CREDENTIALS"
chmod 600 "$CI_CONFIG" "$CI_CREDENTIALS"
```

宿主 Python 建议 3.11 或以上；若系统缺 Python/venv，先通过实际发行版的公司源或离线包补齐。这个 venv 运行 Harness；容器内另有 seed Python，编译依赖安装在镜像里。JSON 不会展开 `$HOME` 或上述变量，配置字段必须填展开后的绝对路径。

## 4. 填写服务器配置与凭据

填写第 3 步已复制的两个配置文件，保持归属 `jiwang_ci`，含凭据文件权限为 600，不重复复制覆盖。补齐当前部署必需的地址、路径、版本和资源值，可选参数按真实环境决定；只配置本次已准备好的版本，其余版本明确记录为未部署。Triton 3.0 必须具备后端和性能能力，其他版本仅前端能力，不能把 3.0 缺失后端改为禁用以通过预检。

| 配置 | 填写规则 |
| --- | --- |
| `state_dir/control_root/codex_home` | 使用该用户所有的实际绝对路径；独立新 state、干净控制 checkout、公司专用 Codex 配置来源，与个人默认 `~/.codex` 分离。 |
| `worker_id` | 本次部署的唯一标识；由交付方提供或记录供其后续配置网关，本窗口不修改 GitHub。 |
| `python_bin/codex_bin` | 前者是宿主 Python，后者是镜像内 Codex CLI 的实际绝对路径；Dockerfile 不会自动联网安装 Codex。 |
| `identities` | 沿用模板的四个不同非 root UID 和两个 GID；它们是容器内数字身份，无需运行 useradd 创建对应宿主账号，也不要为简化部署把它们填成同一个 UID。 |
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

### 将 docs/build 的依赖落实到镜像

| 构建文档的内容 | 当前 CI 如何使用 |
| --- | --- |
| Ubuntu 24.04 | 可作为镜像基础系统；不是要求重装宿主系统。来源与 digest 以实际提供为准。 |
| `build-essential cmake ninja-build git python3 python3-pip python3-venv python3-dev libz-dev libzstd-dev libxml2-dev` | 安装到可信基础镜像，APT 源换成公司可达源或使用离线包。 |
| `/opt/venv`、setuptools/wheel/pybind11 | 可沿用该 seed venv 路径；CI 还需要 `build`、`PyYAML`（import 名为 `yaml`）、`pytest`。`uv` 可保留，非必须依赖其公网安装脚本。 |
| LLVM 预编译包 | 使用下方 `llvm.mode=archive`；`docs/build.md` 中示例 SHA 不能替代实际目标版本的 `llvm-hash.txt`。 |
| `LLVM_BUILD_DIR`、`envsetup.sh` | manager 自动设为镜像内 `/opt/local-ci/runtime/deps/llvm-完整SHA`；原文 `/workspace/llvm-release` 是开发默认路径。 |
| wheel 构建、安装、smoke | 由 `validation_commands` 调用真实工具；不用开发目录里的旧 wheel 代替。 |
| 后端集成 TODO | 仍需真实后端构建配置、wheel 规则、envsetup、smoke/JIT 命令和依赖；3.0 另提供 PPL/torch/torch_tpu/FlagGems。 |
| `docker run --privileged` 示例 | 是开发示例，不用于本 CI；容器的创建和身份隔离由 Harness 管理。 |

[docker/build-env.Dockerfile](../../../../docker/build-env.Dockerfile) 可用作基础镜像配方参考，但当前它仍使用默认联网 APT/pip 源，未提供 Codex CLI、PyYAML 和厂商组件，不能直接当作完整 CI 镜像。离线部署时补齐这些材料和依赖，使用公司源或已经下载的包；不把模型凭据烘焙进镜像。

如果已有可信的完整基础镜像 tar，可直接导入该用户的 Docker。先填 `CI_FOUNDATION_ARCHIVE`、交付方给出的包 SHA256 `CI_FOUNDATION_ARCHIVE_SHA256`，以及预期 Docker 镜像 ID `CI_FOUNDATION_IMAGE`，再执行：

```bash
: "${CI_FOUNDATION_ARCHIVE:?填写本地镜像 tar 的绝对路径}"
: "${CI_FOUNDATION_ARCHIVE_SHA256:?填写交付方提供的镜像包 SHA256}"
: "${CI_FOUNDATION_IMAGE:?填写预期 sha256 格式 Docker 镜像 ID}"
printf '%s  %s\n' "$CI_FOUNDATION_ARCHIVE_SHA256" "$CI_FOUNDATION_ARCHIVE" | sha256sum --check -
docker --host "$CI_DOCKER_ENDPOINT" image load --input "$CI_FOUNDATION_ARCHIVE"
docker --host "$CI_DOCKER_ENDPOINT" image inspect "$CI_FOUNDATION_IMAGE" --format '{{.Id}}'
```

检查成功后，把这个基础镜像 ID 填到对应 `profile.image`。普通 LLVM/PPL 压缩包不是 Docker 镜像，不能 `docker load`；它们由下面的 archive 配方装入最终镜像。

### 用户放好 LLVM/PPL 包以后怎样配置

用户只需提供本地包的绝对路径；**当前 JSON 并不支持只填路径直接运行**。部署窗口负责检查文件存在且可读，用 `sha256sum 实际文件` 计算摘要，检查归档目录层级，并将这些值写入配置。若下载来源提供校验值，再进行比对；本地计算的摘要用于固定这份文件，不能单独证明来源。用户以后替换包时必须重新计算摘要并准备镜像，不能让内容变化沿用旧记录。

LLVM 的 Git commit 需要从包名、随包说明或构建记录确认，与目标源码的 `llvm-hash.txt` 比对；不能从 SHA256 反推，也不能直接把目标 hash 填作不明包的来源证明。没有可确认的版本记录时，只需补这一项材料，不要求用户手填整套 JSON。PPL 和厂商 wheel 同样要检查 CPU 架构、Python ABI 和 runtime 兼容性；单独一个 PPL 文件不能代替完整后端依赖。

部署窗口对用户提供的包可先执行下列只读操作。`CI_DEPENDENCY_ARCHIVE` 填实际 tar 包路径；如果是 wheel/zip，使用对应格式的列表工具，不执行未知包内脚本：

```bash
: "${CI_DEPENDENCY_ARCHIVE:?填写用户提供的本地依赖包绝对路径}"
test -f "$CI_DEPENDENCY_ARCHIVE"
test -r "$CI_DEPENDENCY_ARCHIVE"
sha256sum "$CI_DEPENDENCY_ARCHIVE"
tar -tf "$CI_DEPENDENCY_ARCHIVE"
```

LLVM 配方结构如下，空串需填实际值。当前用户模板已采用 archive 模式，不会默认从源码编译 LLVM：

```json
{
  "mode": "archive",
  "archive": "",
  "sha256": "",
  "commit": "",
  "strip_components": 1
}
```

`archive` 是宿主可读的包路径；`commit` 必须等于该 profile 的 `llvm_hash`。manager 校验摘要后解包到镜像内 `deps/llvm-完整SHA`。`strip_components` 要按实际包结构填写：若包是 `llvm-release/bin、include、lib`，通常为 1；如果顶层直接就是 `bin、include、lib`，通常为 0。错填会导致 include/lib/bin 路径缺失，不能靠修改记录绕过。

如果 PPL 以归档依赖接入，可在 profile 中增加：

```json
{
  "archives": {
    "ppl": {"archive": "", "sha256": "", "strip_components": 1}
  },
  "env": {"PPL_ROOT": "/workspace/deps/ppl"}
}
```

这是**合并片段**，不是完整 profile；不要覆盖已有 `env` 的其他键。上述 PPL 路径只是统一解包到 deps/ppl 时的逻辑示例，厂商若要求子目录则按实际调整。wheel 包使用已确定的 seed Python 安装，不能当 tar 直接套用解包配置。源码依赖依然通过固定 commit 的 Gitee repository 进入配方。

**检查点：** 基础镜像在 `jiwang_ci` 的 daemon 中可见；LLVM 包提交匹配、归档结构正确；PPL/厂商 wheel 与 Python/仿真版本匹配；公司 CLI 已安装在基础镜像中，或由可信 `prepare_commands` 从本地材料安装。不需要人工在未来的 PR 容器里逐次装这批公共依赖。

保留公司实际 provider/model/config.toml/auth.json。它们位于宿主私有来源，运行时仅进入任务的 Codex 私有目录，不写入 Git、镜像层或普通测试环境。仅补充实际 provider 的 `env_key/env_http_headers` 所引用变量。此部署不配置 SMTP；GitHub PR 评论和状态发布链路保持不变。

`--credentials-env` 指定安装后服务读取的文件，不会替安装器当前进程加载变量。手动执行预检和安装前，在 `jiwang_ci` 会话中加载同时兼容 shell/systemd 格式的私有文件，不开启 xtrace：

```bash
: "${CI_CREDENTIALS:?填写私有 EnvironmentFile 的绝对路径}"
set -a
. "$CI_CREDENTIALS"
set +a
```

### Codex 执行模式与四个身份

`agent_ci/codex.py` 会从公司来源保留实际模型/provider/auth，生成任务专用配置。新建和恢复都由驱动设置 `sandbox_mode="danger-full-access"`、`approval_policy="never"`，启用 Shell、unified exec 和可用的编辑能力。**部署时不需要修改公司 auth.json，也不要向 local-ci.json 增加一个程序不读取的 sandbox 配置字段。** 用户服务启动 Harness，Harness 再在 PR 容器中启动 Codex；不用另装一个宿主 Codex service。

| 容器 UID（模板默认） | 写入范围与作用 |
| --- | --- |
| `codex=11004` | Codex 会话及 `/codex/workspace/candidate/` 的原生探索副本。 |
| `candidate=11001` | `/task/candidate/` 的正式候选源码、安装和构建状态。 |
| `base=11002` | `/task/base/` 的正式基线状态，避免候选执行改写对照。 |
| `diagnostic=11003` | MCP 诊断和实验目录；正式候选/基线只读，不能以诊断修改正式安装。 |

这四个身份不对应四个 Agent 或四份常驻进程；身份数量本身不增加常驻内存。代码目前依赖它们进行文件写保护和按 UID 清理，不能只改数字合并。原生命令与 Codex 同身份，可以接触任务模型认证；它没有正式测试身份的凭据隔离。`danger-full-access` 不取消只读镜像和非 root 权限，系统组件仍在可信镜像准备阶段安装。

原生探索副本有独立 checkout、venv、缓存以及 3.0 的 backend；不会自动使用正式检查已安装的候选 wheel。启动上下文中的 `environment_setup` 给出需要 source 的路径和参数，默认不自动运行，便于诊断初始化失败。后端原生实验需先核对初始化、Python 和库路径；构建或安装实验结束后再安排正式检查，避免资源争用。正式通过及阻断复现仍由 MCP/Harness 核验。

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

配置检查输出 `ready: true` 后才构建镜像；失败时按 `checks` 中具体 `check/message` 补配置。这里要求 Gitee 任务/结果和 health 发布凭据；SMTP 全空时通过，部分配置仍报错，不发信。`rotate.py` 成功应返回 `state: ready`、`validated: true`、`image_id` 和 `release_id`；记下它们，不手工修改登记文件。

若想把某条命令的输出存为部署日志，可以在同一 Bash 中先 `set -o pipefail`，再将命令加上 `2>&1 | tee "$CI_LOG_DIR/本步名称.log"`。不要只看 tee 是否成功；命令退出状态和 JSON 检查项都要通过。镜像构建和自检的详细日志还会自动写入 `state/environments/image-logs/发布ID.log`。

逐条执行，失败则停止。对配置中每个 profile 完成镜像准备后再执行：

```bash
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG" --probe-runtime
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG"
```

资源预检检查实际 CPU/memory/pids 限制和镜像身份。公司模型配置、Gitee/health 凭据或其他必需项缺失时如实记录，不能绕过正式预检安装，不能宣称部署完成。SMTP 未配置不阻塞部署，watchdog 仍维护异常、恢复和健康输出，但跳过邮件且不积压邮件队列。预检通过不代表真实模型、PR 或 GitHub 链路已验收。

镜像自检会检查 Codex CLI 可运行；实际启动驱动还会读取 `codex features list`，确认 Shell/unified exec 功能可用，已移除的功能开关不会重新开启。这不是模型调用成功的证明，本轮不通过运行 PR 或调用模型来补验收。

**检查点：** 正式预检输出 `ready: true`，证明保存在 `state/deploy/runtime-probe.json`。profile、控制版本、镜像或资源配置改变后需要重新生成匹配证明。配置中的资源限额由任务/验证容器使用；目前 `docker build` 调用没有同样的 CPU/memory/pids 参数，不能把此 probe 当成镜像构建过程的资源证明。基础镜像准备或源码编译时，需按批准额度配置 CI 用户/构建器资源；本次使用预编译 LLVM 可以减少这部分编译负担。

## 6. 安装用户服务，保持未接单

确认 `jiwang_ci` 下没有同名活动 CI 服务/定时器，填写独立 `CI_REVIEW_DIR`，先渲染检查再安装：

```bash
: "${CI_REVIEW_DIR:?填写服务渲染目录的绝对路径}"
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR"
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR" --apply
```

安装器生成 Worker、health、retention 和已配置版本的镜像轮换用户服务/定时器，备份已有文件并执行用户级 daemon-reload；不会启动服务。检查生成文件中的运行路径、EnvironmentFile、Rootless endpoint、`NoNewPrivileges=yes` 和定时配置，记录安装器返回的备份目录。任务目标分支与环境 profile 不同名时，在私有配置的 `branch_profiles` 中显式映射；当前切换点只允许 `CI_dev` 映射到 `triton_v3.0`，不能用默认 profile 或可变标签兜底。

**检查点：** 安装输出 `services_started: false`；用下面的只读命令确认 unit 已安装且未开始接任务：

```bash
systemctl --user list-unit-files 'triton-anchor-local-ci*'
systemctl --user show triton-anchor-local-ci.service --property=LoadState,ActiveState,SubState,NoNewPrivileges
systemctl --user list-timers --all 'triton-anchor-local-ci*'
```

新安装的 Worker 应为 loaded/inactive，CI timer 不应 active。Worker 是常驻轮询服务，**没有 Worker timer**；health/retention/各版本镜像轮换才使用 timer。这与旧版“timer 周期启动 pull-and-run 脚本”有区别，不要再给新 Worker 额外套一个轮询 timer。

本轮不启用或启动这些 CI unit，不运行 `worker.py --once`，不迁移生产队列、不停旧 CI。Worker 一旦启动会自动处理有效任务；health/retention 等定时器也会产生发布或清理操作，因此统一留到后续启用。本轮无需构造正常或失败 PR，也无需 GitHub Gateway 操作。

## 后续自己调试时，从哪一步查起

先判断失败发生在账户/Docker、镜像准备、预检还是服务阶段。保留当前日志，修正该阶段配置后重做对应步骤；不要通过删除整个 state 或手动把 validated 改为 true 来恢复。

| 现象 | 先看什么 | 从哪里继续 |
| --- | --- | --- |
| `Failed to connect to bus`、Docker socket 不存在 | 当前用户、登录会话、Docker 用户服务日志 | 第 2 步修复 user systemd/Rootless；不切换到系统 Docker。 |
| 镜像找不到，另一个用户却能看到 | 当前 `--host` 与该 daemon 的 `image inspect` | 第 4 步向正确的 Rootless daemon 导入。 |
| `yaml` / `build` / `torch_tpu` import 失败 | seed Python 的实际路径、基础镜像中的安装环境；镜像日志 | 第 4 步补依赖或可信 prepare_commands，然后重新 rotate。 |
| LLVM 摘要/提交不匹配，include/lib 不存在 | 包 SHA256、llvm-hash.txt、strip_components | 修正 archive 配方或更换匹配包，然后重新 rotate。 |
| Backend rebuild/smoke 失败 | 3.0 镜像日志、厂商版本、BACKEND_*、PPL_ROOT | 修复实际依赖和命令，保留 backend_enabled=true。 |
| `runtime_probe` 缺失或过期 | 资源/配置/控制提交和活动镜像是否变化 | 镜像需要更新时先 rotate，再执行 --probe-runtime 与正式预检。 |
| 安装失败：凭据权限或 Gitee/health 缺失 | 文件归属/600 权限、当前进程是否已加载 EnvironmentFile | 第 4 步补齐，再做正式预检和安装；SMTP 全空不算失败。 |
| unit 已安装但没有任务日志 | `ActiveState`，本轮是否仍处于未启用状态 | 本轮 inactive 属于预期，不为制造日志而启动 Worker。 |
| 更新后仍显示 read-only 或原生命令不可用 | 宿主控制 SHA、镜像 control_revision、实际 Codex CLI 能力 | 按下方更新步骤准备匹配版本；不只改 unit 或公司配置来源。 |
| 原生 Python/JIT 缺库，但正式工具能运行 | 原生 venv 是否已安装候选 wheel，是否加载 environment_setup，库路径是否指向探索 backend | 在任务副本中初始化和排障；不用实验结果替代正式检查，也不修改正式目录权限。 |
| 原生证据导出失败，任务数据未回收 | 私有 native 记录的 export.json、环境事件和工作区健康状态 | 修复空间/权限/容器可达性后让恢复逻辑补导出；不先删除数据卷。 |

常用的本地诊断命令如下，均不投递 PR、不上传健康结果：

```bash
journalctl --user -u docker.service -n 100 --no-pager
journalctl --user -u triton-anchor-local-ci.service -n 100 --no-pager
"$CI_PYTHON" "$CI_CONTROL/scripts/local_ci/environments/manager.py" --config "$CI_CONFIG" health
ls -lt "$CI_STATE/environments/image-logs"
```

`manager.py health` 读取镜像/任务登记，不能单凭它判断 Docker 真正可用；结合 Docker info 和预检查看。对某个镜像问题，用 `tail -n 120 "$CI_STATE/environments/image-logs/实际发布ID.log"` 查看构建、自检输出。`docker logs` 只看容器入口输出，不能替代 `docker exec` 工具检查的日志。

宿主的重要文件是 `state/environments/registry.json`（镜像与任务容器登记）、`state/environments/events.jsonl`（环境事件）、`state/deploy/runtime-probe.json`（资源证明）、`state/deploy-backups/`（unit 备份）。后续真正接单才会有 `state/journal.sqlite3` 等任务记录；不要手改数据库。日志可能含内部路径，分享前脱敏，但不打印凭据文件。

后续接单后，每次运行的宿主记录位于 `state_dir/tasks/<task_id>/<run_id>/`，不是容器内的 `/task`。在该目录下查：

| 相对路径 | 用途 |
| --- | --- |
| `task.json`、`policy.json`、`skill-manifest.json` | 冻结任务、最低检查和实际加载的 Skill 摘要。 |
| `codex-session.json`、`codex-events-<id>.jsonl` | 会话恢复身份和 CLI 原始事件；原始事件未保证脱敏，保持私有。 |
| `native/codex-events-<id>/actions.jsonl` | 原生命令、输出、退出状态和编辑事件索引；已替换已知凭据文本，仍只供私下排障。 |
| `native/codex-events-<id>/context.json`、`export.json` | 探索记录所属 attempt、导出是否完成或数据是否丢失；异常时 export.json 可能尚未生成。 |
| `native/codex-events-<id>/workspace/native-manifest.json` | 有界源码变更快照；中断恢复可能位于 `recovered-*`，以 export.json 的 destination 为准。清单注明排除的依赖、缓存、凭据等目录/文件，不是整个环境备份。 |
| `published/result.json`、`published/evidence/` | 已封存的正式结果和证据，供 Harness 上传 Gitee。 |

原生记录不自动上传 Gitee，也不能满足最低检查。它们与正式证据分开持久保存，不计入任务 scratch 的 100 GiB 预算；部署者需把这些宿主私有记录计入磁盘规划。成功封存先完成测试身份清理；Codex 随后停止并导出原生变更。封存后的清理/导出异常会保留数据并报告运维异常，不改写已封存结果。

需要撤销本次 unit 安装时，使用安装输出的备份路径，先运行 `install.py --rollback 实际备份路径` 查看计划，再加 `--apply`。它只回退 unit，不回退配置、镜像或任务状态；完整迁移/回滚另见上级 README。

## 已有部署如何更新到本版

仅在后续明确安排升级时执行；当前部署窗口仍只安装、不接单。升级需要更新可信控制代码和匹配的 CI 镜像，不能只改 Codex 参数或复用旧运行容器。

1. 先安排旧任务收尾，停止接单，备份控制版本、私有配置、state/outbox 和必要任务数据。Skill 摘要已变，旧会话不能用新版规则强行 resume；未完成任务在其匹配版本收尾，或明确取消后重新投递。
2. 从交付方的 Gitee ref 取得干净、完整 SHA 的控制 checkout。保留公司 provider/model/auth；核对配置字段，不覆盖已有私有文件。GitHub 投递的 worker_revision_sha 也须匹配，由交付方负责协调，服务器窗口不访问 GitHub。
3. 按第 5 步为各 profile 重新 rotate，生成包含新版 Harness/MCP/容器管理代码的镜像，再运行资源 probe 和正式预检。仅更新宿主文件不会更新镜像内控制代码。
4. 按第 6 步重新渲染并审阅用户 unit，记录备份和新版本；本轮仍保持未启用。回退时恢复匹配的代码、配置、状态和镜像，不能仅恢复旧 unit 或某个沙箱参数。

## 部署窗口的最终交付

只提交一份简洁部署记录：

- 实际 OS、用户/UID、目录及资源额度；Rootless daemon 状态和资源预检结果。
- Gitee 控制仓库/ref/完整 SHA，实际 worker_id 和配置文件路径（不含凭据内容）。
- 已准备的版本、镜像发布 ID/digest、LLVM/PPL/后端来源版本；未部署版本和缺失材料。
- 预检日志、已安装 unit 清单、安装备份位置；明确 CI 服务/定时器仍未启用接单。
- 记录代码已包含容器内 danger-full-access、新建/恢复加载逻辑和原生证据目录；这只是部署能力核对，不能记作真实 Codex 审查已通过。
- 若存在阻塞，列出已完成部分和缺项；明确未做 PR 试跑、真实模型调用、邮件发送及 GitHub 侧配置或验收。

上级 [部署 README](../README.md) 包含完整产品的运维、迁移和回滚参考；本轮执行范围以本文为准，不自动扩展到其中的上线与联调步骤。
