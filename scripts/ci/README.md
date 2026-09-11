# Local CI Gateway 与接收器

`ci-gateway.yml` 冻结 PR 的 base/head 和精确 merge SHA，执行前置检查，按需等待外部 fork 的 environment 审批，再复查 PR。源码和子模块固定 refs 全部推送到 Gitee 后，才用同一个控制提交发布不可变 `tasks/`、当前 `current/` 和取消 `cancel/`。源码 refs 含完整 task ID，重试不能移动另一任务的源码。

`ci-receiver.yml` 是每五分钟或手动运行的短作业，最多十分钟。它从固定控制 SHA 加载接收器，检查 Gitee 结果、受信最低集合、执行/工件对应、必要附件交付和 PR 当前身份，更新同一条 PR 评论、四个稳定门禁与业务 Dashboard。Worker 不连接 GitHub。健康采集和 watchdog 在 CI 主机的 `ops_maint/` 独立运行。

部署时配置：

| 配置 | 含义 |
| --- | --- |
| `LOCAL_CI_CONTROL_REF` | 受保护的控制发布分支或标签短名称，不能跟随开发分支 HEAD |
| `LOCAL_CI_CONTROL_SHA` | 该发布 ref 对应的完整 40 位 SHA，与 CI 主机控制版本一致 |
| `LOCAL_CI_TARGET_BRANCHES` | 允许测试的目标分支 JSON 数组，默认 `["main"]` |
| `GITEE_RESULTS_REPO_URL` | CI 专用 HTTPS Gitee 仓库，承载固定源码 refs、控制和小结果 |
| `GITEE_SUBMODULE_MIRRORS` | 子模块路径到 Gitee 镜像 URL 的 JSON 映射，例如 `{"FlagGems":"https://gitee.com/OWNER/FlagGems.git"}` |
| `GITEE_USERNAME` / secret `GITEE_TOKEN` | 该仓库的 Git 和 Release 附件权限 |
| `local-ci-fork-approval` environment | 外部 fork 审批；必须实际保存非空 required reviewers |

控制版本升级先暂停派发、排空或取消未封存任务，再同步发布 ref/SHA 与 Worker。封存结果可继续单独补传。接收器运行的分支不决定控制程序版本。

GitHub required checks 必须实际配置并查询验证：`local-ci/basic`、`local-ci/api`、`local-ci/security`、`local-ci/summary`。前三项是 PR head 上的 Check Runs，summary 是 commit status，避免同一名称同时对应两种门禁。跳过和取消的前置检查按失败门禁处理。封存 pass 在必要附件 pending 时，summary 保持 pending；网络失败不会改写测试结论。

结果仓库每次运行只增加 `result.json`、`execution-summary.json` 和独立可更新的 `delivery-index.json`；每个小 JSON 最大 2 MiB。必要日志/报告经 gzip 压缩为最多 32 MiB 的 Release 附件；大报告按序拆分，合并所有 part 后解压。超大日志仅公开标注过省略范围的首尾片段，完整日志保留在宿主。超过预算的可选 wheel 直接记 omitted。上传响应丢失时先查询附件，再核对下载内容的大小与 SHA-256，避免重复上传。

必要证据已交付后，Worker 每轮最多为一个已 published run 补传 pending 的可选附件；单个 run 默认间隔五分钟（`optional_delivery_retry_seconds`，最少一分钟），默认发布满 30 天停止（沿用 `results_retention_days`）。历史 run 也可补传，失败保留 published，不启动 Agent、容器或测试。到期标记和重传共用 run 的交付锁，expired 附件不再上传。

本地回归：`python3 -m unittest discover -s scripts/ci/tests -v`。测试使用本地 bare Git 和模拟附件服务，不发布真实结果。上线仍需在目标仓库验收 Gitee 附件权限/配额/下载/删除、GitHub environment 审批、required checks 实际生效、Pages 托管与完整 PR 闭环；仓库中的配置文件不能代替这些平台验收。
