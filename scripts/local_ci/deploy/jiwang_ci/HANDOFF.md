# jiwang_ci 服务器部署交接

交接对象：能够检查并操作目标服务器的部署对话。唯一已确认服务器信息是 CI 用户名 `jiwang_ci`；发行版、家目录、UID、资源额度、镜像、公司依赖、模型来源和中转地址待检查，本材料没有填入或猜测实际值。

实施依据：当前 CI_dev 的 PR 独立 Rootless 任务容器方案。宿主 jiwang_ci 运行 Harness 和 Rootless Docker；每个任务容器内运行单一 Codex 和确定性工具，candidate/base/diagnostic/Codex 四个非 root UID。CI 用户可有人工维护用的 sudo 权限，自动 CI unit 使用 NoNewPrivileges=yes，不调用 sudo。不新建其他宿主执行用户，不接入系统 Docker 组，不改动其他人的容器、服务或全局 Docker 数据。

## 操作边界

- 本交接文件是部署说明，不是本地 CI 的运行期 Skill。运行期唯一 Skill 是 scripts/local_ci/skills/local-ci/SKILL.md。
- 代码推送由用户负责。允许仓库仅 likehupochuan/triton-anchor；只涉及 CI_dev、main 或用户允许的新分支。不操作 CI_dev_forPR，不对 RACE-org/triton-anchor 执行远端操作。
- 服务器无法直接访问 GitHub；控制代码、任务和依赖经公司可达 Gitee、内部镜像或离线包取得。现有 Gitee PR 代码 refs 不保证包含 CI_dev 控制分支，必须单独核实。
- 沿用公司实际 Responses provider/model/config.toml/auth.json，不将凭据提交进 Git、镜像层或普通测试环境，不在聊天中打印凭据。
- 保留单向上传：Codex 封存结束，Harness 上传 Gitee 后 complete；GitHub 独立 status→comment→Pages，不增加 receipt。不改分支保护。
- 首轮建议只接入 Triton 3.0，完成正常 PR 和故意失败 PR 后扩展 3.3/3.6。3.0 后端能力不可降级；其他版本仅已有前端能力。

## 1. 只读盘点，补齐实际配置

确认 getent passwd jiwang_ci、id jiwang_ci、发行版/内核/systemd、磁盘文件系统/容量、允许的 CPU/内存/PID 额度；确认旧 CI 用户、服务、timer、队列和状态格式。检查现有 Docker 是系统还是 rootless、哪些工作属于其他用户，禁止执行全局 prune、停系统 Docker 或删除公共 socket。

核对现有 3.0 可信基础镜像的来源与不可变 digest、seed Python 和 Codex CLI 路径、LLVM 精确 commit/构建配置或预编译包 sha256、PPL/torch/torch_tpu、仿真后端、FlagGems 及测试命令。用户说后端是仿真，不预设物理设备；当前镜像验收会实际检查 torch/torch_tpu/PPL 和后端构建、smoke，不能只凭“有 LLVM 和 PPL”声明能力齐全。

确定 Gitee 代码/任务结果仓库、健康仓库与现存分支、权限、公司 CA/代理、模型专用凭据来源、SMTP 可达性。不要把存放 SMTP/health 配置等同于已经发送邮件或验证连通性。

将本目录 config.template.json 复制到 Git checkout 外并填实际值。它是完整模板，所有空值都必须解决；初期只测 3.0 时移除未准备的 profiles，避免预检要求另外两个镜像。

| 配置 | 填写规则 |
| --- | --- |
| state_dir/control_root/codex_home | 确认实际家目录后选择 jiwang_ci 所有的绝对路径；控制目录为完整干净 Git checkout；Codex 来源与个人默认 ~/.codex 分离。 |
| worker_id | 独立试验 worker 标识；与 GitHub LOCAL_CI_WORKER_ID 一致。 |
| runtime.endpoint/context | 真实 jiwang_ci UID 的 unix:///run/user/UID/docker.sock，context 指向同一 endpoint；不填模板 UID。 |
| resources | 经服务器额度确认后的 cpus、memory_bytes、pids_limit；max_jobs 默认8，可降低，不能按整机总量自动占满。 |
| profiles 的 key | 必须等于 task.target_branch，即 PR 目标分支。模板 triton_v3.0 等须核实；若目标是 main/CI_dev，应配置实际版本对应的 profile，不能假定按版本自动路由。 |
| profile.image | 可信基础镜像 digest；已有系统 Docker 镜像不会自动出现在新用户 rootless daemon 中，需可信拉取或按来源/摘要导入。禁止从跑过 PR 的容器 commit 成基础镜像。 |
| profile.repositories | 以目录名为 key，每项 repository 为公司可达源、commit 为精确 SHA；镜像内落在 /opt/local-ci/runtime/目录名。至少准备镜像自检所需 triton-anchor；3.0 另需真实后端/FlagGems 来源。 |
| profile.archives | 可选预编译依赖，以名称为 key，每项 archive/url、sha256、strip_components；镜像内落在 /opt/local-ci/runtime/deps/名称。 |
| profile.llvm | source：repository+可信cmake_args；archive：archive/url+sha256+commit，commit必须等于llvm_hash。llvm_hash以冻结被测代码 triton/cmake/llvm-hash.txt 为准。 |
| profile.env | seed venv、后端/envsetup/测试命令、PPL和FlagGems实际路径；workspace_container默认/workspace是配方逻辑前缀，manager会改写该前缀为/opt/local-ci/runtime，不能当宿主挂载目录。 |
| codex_bin | 镜像内已安装的实际 Codex CLI 绝对路径；Dockerfile不会自动从互联网安装 Codex，基础镜像或可信 prepare_commands 必须提供它。 |

基础镜像还须具备实际编译器/CMake/Ninja、Git、Python、venv和构建依赖；seed Python 能 import build/setuptools/wheel/pybind11/yaml/pytest，3.0 另需匹配的厂商 runtime。依赖使用公司可达包源或校验过的离线包。

validation_commands 是真实 argv 数组。前端至少 environment/frontend_build/wheel_install_import/frontend_smoke，3.0 再加 backend_rebuild/backend_smoke_jit；可加完整10工具验证。示例命令结构：

```json
["python3", "/opt/local-ci/control/scripts/local_ci/deploy/validate_environment.py", "environment"]
```

将工具名换成对应检查 ID，使用真实镜像 Python；工具读取镜像配方里的 triton-anchor/后端 checkout 和 env，不能用 echo/true 代替。运行 PR 仍由可信最低检查策略决定完整检查集合。

## 2. 建立账户和 Rootless runtime

若 jiwang_ci 不存在，由管理员按实际发行版创建。可授予人工 sudo；自动服务始终以普通用户运行。安装实际发行版需要的 uidmap/rootless extras、用户 D-Bus/systemd，配置不与现有用户冲突的至少65536 subuid/subgid，以及 linger。

通过真实 jiwang_ci 登录会话安装/启动 rootless docker user service。若系统 Docker 服务属于其他人，保留它；安装器要求时只使用 rootless setup 的 --force 共存选项，不停系统 Docker。Docker data-root 使用支持的本地文件系统，不用 NFS。

检查 docker --host ACTUAL_ROOTLESS_ENDPOINT info：必须 rootless、cgroup v2、systemd；CPU/memory/pids delegation 由管理员只针对实际需要配置，不能假定资源参数已生效。正式 probe 会验证实际 cgroup 数值。

官方依据：[前提和安装](https://docs.docker.com/engine/security/rootless/)、[用户服务与资源控制](https://docs.docker.com/engine/security/rootless/tips/)。

## 3. 取得控制代码并核对版本

先确保 GitHub 上 CI_dev 和必要的 main 路由是本次交付版本；旧接单保持受控。公司 Gitee 有可信 CI_dev 镜像时从那里拉取；没有时由能访问 GitHub 的机器提供该分支 git bundle，再由服务器导入。不要把普通 ZIP 当完整控制仓库，控制版本校验需要 Git HEAD。

核对服务器 git rev-parse HEAD 与 GitHub 网关冻结的 worker_revision_sha 完全一致，git status --porcelain 为空。安装依赖和私有配置不得写脏控制目录。代码更新会改变可信镜像配方；处理在途任务后重建镜像和 probe。

## 4. 凭据与 GitHub 配置

credentials.env.template 仅列出变量名。复制到可信配置目录、由 jiwang_ci 所有、chmod600，再在服务器私下填写。公司 config.toml/auth.json 同样私有；只有 provider 明确需要时补其 env_key/env_http_headers 所引用变量。

手动运行预检/worker/install时必须先将 EnvironmentFile 的值加载到当前进程环境；--credentials-env只告诉安装器service今后读取哪个文件，安装器不会替当前进程加载凭据。不要开启shell xtrace或回显文件。使用既支持shell也支持systemd的赋值格式，可在服务器上：

```bash
set -a
. "$CI_CREDENTIALS"
set +a
```

GitHub Actions variables：GITEE_RESULTS_REPO_URL、GITEE_USERNAME、LOCAL_CI_HEALTH_URL、LOCAL_CI_WORKER_ID；secrets：GITEE_TOKEN、私有心跳读取用GITEE_HEALTH_TOKEN、实际LOCAL_CI_SMTP_*。健康发布仓库的health_branch必须已经存在；HEALTH_URL指向其中worker-health.json的实际可读取地址。服务器健康发布token需要写权限，GitHub读取可使用单独只读token。

配置 local-ci-fork-approval 的真实required reviewers和 github-pages/Pages Actions发布源；不改分支保护。测试中用独立Gitee任务/结果仓库与健康仓库可避免消费历史队列；GitHub变量必须与新worker指向一致。若使用同一生产队列，必须先停止并排空旧CI，不能让两个worker竞争。

## 5. 构建镜像、预检、首轮试跑

以下变量由部署窗口填写实际路径，全部在 jiwang_ci 会话中执行。CI_PYTHON 为实际宿主Python解释器（建议3.11+），CI_CONTROL为干净控制checkout，CI_CONFIG为已填写的JSON，CI_CREDENTIALS为私有EnvironmentFile；任何变量未填时不执行命令。

```bash
cd "$CI_CONTROL"
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG" --configuration-only
"$CI_PYTHON" scripts/local_ci/deploy/rotate.py --config "$CI_CONFIG" --profile triton-3.0
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG" --probe-runtime
"$CI_PYTHON" scripts/local_ci/deploy/preflight.py --config "$CI_CONFIG"
```

顺序执行，前一步失败就调查该错误，不跳过。profile名称若调整，命令同步修改。缺SMTP等配置时可用 --skip-notifications 做局部开发预检，但正式install不接受把它当完整预检通过。预检不会验证真实模型响应。

先在 likehupochuan 仓库中准备测试 PR，用 GitHub CI Gateway 的 workflow_dispatch：mode=request、pr_number=实际PR编号、full=false。不要直接选择mode=run/service绕过路由，它们要求匹配的worker SHA。确认 review card 和门禁完成、Gitee出现冻结任务后，旧worker已停或队列隔离，再执行：

```bash
"$CI_PYTHON" scripts/local_ci/agent_ci/worker.py --config "$CI_CONFIG" --once
```

--once 会处理该队列所有当前有效任务，不是“只跑一个PR”。因此首轮队列只放预期测试任务。首次包含真实公司模型调用、编译/后端测试和Gitee结果上传；执行前由部署窗口明确汇报实际目标与资源额度。不要用生产PR制造失败。

## 6. 服务安装与切换

首轮检查后渲染用户级服务，再安装；安装器不启动服务：

```bash
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR"
"$CI_PYTHON" scripts/local_ci/deploy/install.py --config "$CI_CONFIG" --credentials-env "$CI_CREDENTIALS" --render-dir "$CI_REVIEW_DIR" --apply
systemctl --user enable --now triton-anchor-local-ci.service
systemctl --user enable --now triton-anchor-local-ci-health.timer
journalctl --user -u triton-anchor-local-ci.service -f
```

确认首轮正常PR、故意失败PR和连续任务清理通过后，启用已准备版本的 triton-anchor-local-ci-environment-triton-3.0.timer，以及 triton-anchor-local-ci-retention.timer；另两个版本就绪再启用对应timer。首次切换旧队列/旧状态参考上级README迁移流程，旧SQLite快照格式不兼容时不能强行导入，不直接修改旧数据库。

## 验收与交付给用户

- 正常 PR：冻结SHA一致，Codex实际通过MCP执行工具，最低检查及审查齐全，结果封存并上传Gitee；GitHub随后独立status/comment/Pages发布。
- 故意失败PR：可定位真实失败，Codex可以继续诊断或做独立实验；不虚报通过，实验结果不替代正式结论。
- 连续两个PR：不同attempt/container/data/session身份；旧进程和安装状态不串用，失败证据按策略保留。
- 资源：实际cpu.max/memory.max/pids.max等于配置；观察编译峰值与OOM恢复，不影响其他用户工作。
- 上传故障：修复后只重发封存结果，不重进模型/构建。GitHub发布与本地上传分开验收。
- 健康：独立心跳可读、离线/中转异常分类正确；实际发邮件需单独明确作为验收操作，不能把SMTP字段校验称作发信成功。

记录实际服务器OS/用户UID、控制提交、镜像digest、LLVM/PPL/后端版本、脱敏配置位置、服务/定时器、测试PR/task/run及日志路径、回滚备份。最终明确哪些真实验证通过、哪些仍未做；不以581项本机模拟替代服务器验收。
