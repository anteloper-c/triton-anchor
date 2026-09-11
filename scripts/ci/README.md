# Local CI Gateway 与接收器

`ci-gateway.yml` 冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和需要随任务检出的子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。

根目录 `FlagGems` 使用服务器 profile 中预置的依赖，不要求网关提供镜像，也不随任务推送或检出；PR 修改 `FlagGems` 指针不会切换服务器固定依赖。其他子模块仍固定到被测 Git 对象。

旧 `CI_dev` 的分支式源码 refs 记录保留在 Gitee，网关的取消扫描与接收器识别后跳过，不让旧记录阻断新任务，也不据此回写通过；旧记录不列入新版看板当前任务列表。新版任务仍要求完整 task ID 对应的固定 refs，损坏记录不会被当成旧格式忽略。

`main` 仅保留路由和 `ci-receiver.yml` 接收入口，完整执行流程位于 `local-ci-unified`。每次派发自动读取该控制分支的当前 SHA，写入任务并用于执行校验；所有 PR 目标分支均可派发，push 自动触发仍限于 main。

`ci-receiver.yml` 是每五分钟或手动运行的短作业，最多十分钟。每次运行先解析 `local-ci-unified` 的 SHA，再按该 SHA 加载接收器，检查 Gitee 结果、受信最低集合、执行/工件对应、必要附件交付和 PR 当前身份，更新同一条 PR 评论、四个稳定门禁与业务 Dashboard。Worker 不连接 GitHub。健康采集和 watchdog 在 CI 主机的 `ops_maint/` 独立运行。

部署时配置：

| 配置 | 含义 |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | CI 专用 HTTPS Gitee 仓库，承载固定源码 refs、控制和小结果 |
| `GITEE_SUBMODULE_MIRRORS` | 除根目录 `FlagGems` 外，需要随任务检出的子模块路径到 Gitee 镜像 URL 的 JSON 映射；没有这类子模块时无需配置 |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 该仓库的 Git 和 Release 附件权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批；必须实际保存非空 required reviewers |

无需配置控制分支、控制 SHA 或目标分支列表的 Actions 变量。控制版本升级时仍须同步服务器 checkout；Worker 要求任务中的控制 SHA 与服务器版本一致。各 PR 目标分支仍需在服务器配置对应环境 profile。封存结果可继续单独补传。接收器的定时入口必须位于默认分支 main，github-pages environment 也须允许 main 部署。

GitHub required checks 必须实际配置并查询验证：`local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`。前三项是 PR head 上的 Check Runs，summary 是 commit status，避免同一名称同时对应两种门禁。跳过和取消的前置检查按失败门禁处理。封存 pass 在必要附件 pending 时，summary 保持 pending；网络失败不会改写测试结论。

结果仓库每次运行只增加 `result.json`、`execution-summary.json` 和独立可更新的 `delivery-index.json`；每个小 JSON 最大 2 MiB。必要日志/报告经 gzip 压缩为最多 32 MiB 的 Release 附件；大报告按序拆分，合并所有 part 后解压。超大日志仅公开标注过省略范围的首尾片段，完整日志保留在宿主。超过预算的可选 wheel 直接记 omitted。上传响应丢失时先查询附件，再核对下载内容的大小与 SHA-256，避免重复上传。

必要证据已交付后，Worker 每轮最多为一个已 published run 补传 pending 的可选附件；单个 run 默认间隔五分钟（`optional_delivery_retry_seconds`，最少一分钟），默认发布满 30 天停止（沿用 `results_retention_days`）。历史 run 也可补传，失败保留 published，不启动 Agent、容器或测试。到期标记和重传共用 run 的交付锁，expired 附件不再上传。

本地回归：`python3 -m unittest discover -s scripts/ci/tests -v`。测试使用本地 bare Git 和模拟附件服务，不发布真实结果。上线仍需在目标仓库验收 Gitee 附件权限/配额/下载/删除、GitHub environment 审批、required checks 实际生效、Pages 托管与完整 PR 闭环；仓库中的配置文件不能代替这些平台验收。
