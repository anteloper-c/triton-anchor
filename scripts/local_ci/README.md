# Local CI

AI 驱动 PR 构建、测试与审查。Codex 负责理解意图、选择验证、组织执行及排错；
Poller 负责准备对应环境、停止失效任务和交付结果。源码和结果都经 Gitee 中转。

## 执行流程

```mermaid
flowchart TD
  A[GitHub：PR 校验 → Basic CI → API 兼容性 → Security Gate] --> B[approval review card → 按需人工审批]
  B --> C[GitHub → Gitee：冻结被测提交、base/head 与 PR 信息，投递任务]
  C --> D[Local Poller：校验任务，准备 Triton / LLVM / 后端环境]
  D --> E[Codex：PR 信息校验，解析意图与影响范围]
  E --> F[按需构建与测试，调用基础 tools]
  E --> G[架构契约与意图 / label 专项审查]
  F <--> G
  F --> H[满足最低必检，汇总检查、AI 证据、阻塞项和性能变化]
  G --> H
  H --> I[Local CI → Gitee：保存结果与所选重要文件]
  I --> J[GitHub：校验当前任务，回写 Checks / PR comment / Dashboard]
```

构建、测试和审查按依赖与相关度交错执行。项目规则给出最低覆盖范围，Codex 自主选择
命令、选测与补充用例，并可在任务内修复环境或降低编译并行度重试。
PR 信息与架构审查必须完成，严重高风险发现阻塞；其余风险与有效性能回退报告给维护者。

## 文件结构

```text
scripts/local_ci/
├── AI_CI_PROGRAM.md          # Codex 的职责、工作方式与结果格式
├── agent_ci/                # Poller、原生 Codex CLI、任务身份和 Git 发布
├── prepare/                 # 环境、依赖、任务容器与部署入口
├── maintenance/             # 健康、watchdog 与本地保留清理
├── tools/
│   ├── basic_tools/         # 可复用构建、安装、测试和性能工具
│   ├── ai_review_tools/     # 架构与专项审查说明
│   └── ai_custom_tools/     # 任务内生成工具的使用约定
└── tests/                   # 必要的 Local CI 行为回归
```

GitHub 网关和接收器位于 `scripts/ci/`，页面位于 `dashboard/`。
[AI_CI_PROGRAM.md](AI_CI_PROGRAM.md) 是唯一 Agent 程序入口；[tools/README.md](tools/README.md)
说明调用方法。Codex 原生 shell 可直接运行工具、已有测试及定向脚本，无需 MCP、JUnit 或逐命令回执。
最终以实际结果和必要证据汇总最低检查范围，不重复执行已有有效验证。

## 环境与生命周期

每任务一个 Rootless Docker 容器、一个非 root 用户，Codex 与构建测试共享任务环境。
只读挂载可信控制代码和服务器依赖，源码、venv、编译输出及缓存写入任务目录。
candidate/base 使用冻结提交和独立环境；基线源码已提供，是否构建比较由 Codex 决定。
后端 wheel 在任务内重新构建。`/tmp` 允许动态库加载，编译缓存优先使用 `/task`。

FlagGems 使用服务器 profile 中的固定只读依赖；修改 PR 中的 FlagGems 子模块指针
不会改变该依赖。其他子模块仍通过 Gitee 固定到对应提交。
所有 PR 目标分支都可派发，服务器 `branch_profiles` 为目标分支选择实际环境。

Worker 轮询任务有效性，PR 关闭、转 Draft、更换目标或增加提交时停止旧容器。
Codex 短暂中断可恢复同一 CLI 会话；Worker 重启会清理未完成容器并新建任务运行，
保留之前的本地证据。已封存结果只重试发布，不重编、不重测。

```text
state_dir/
├── runs/<task>/<run>/
│   ├── task.json, state.json, codex-session.json
│   ├── logs/                # 本机完整 Codex 日志
│   ├── artifacts/           # 计划、工具输出和定向用例
│   └── sealed/              # 待发布或已发布结果及所选文件
└── work/<task>/<run>/        # 临时任务目录，结束后清理
```

状态仅区分准备、执行、待发布、已发布。结果与所选文件一次提交到 Gitee 的
`runs/<task>/<run>/`，不使用 Release 附件或额外交付索引。单文件 2 MiB、总计 10 MiB、
最多 20 文件，超出时保留本机并注明；本机完整日志默认保留 30 天。
任务与结果格式只有固定名称，不带版本号；旧格式记录跳过，历史文件不会重新执行。

GitHub 接收器只回写仍对应当前 PR 的结果。已关闭任务终止待定状态，评论给出简短结论、
检查与审查、阻塞原因和证据链接。Dashboard 展示任务、算子、后端、性能与健康状态。

## 部署与验证

填写实际镜像、路径、Gitee 与 Codex 配置后，以普通 CI 用户执行：

```bash
python3 scripts/local_ci/prepare/install.py \
  --config /absolute/path/config.json \
  --credentials-env /absolute/path/credentials.env --apply
```

安装入口准备环境并启动 Worker 与必要维护定时器；不需要工具服务或独立调度控制台。
详见 [服务器准备](prepare/README.md)、[维护](maintenance/README.md)
与 [GitHub 配置](../ci/README.md)。网关自动解析控制分支提交，服务器部署对应干净版本。

本地行为回归：

```bash
python3 -m pytest scripts/local_ci/tests -q
```

需要 Python 3.10+、pytest、PyYAML 和 Git；页面测试还需要 Node.js。
这些检查验证控制逻辑，实际环境仍需在服务器完成 build/install/smoke/JIT 与性能验收。
