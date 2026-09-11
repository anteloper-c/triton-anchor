# Local CI 运维

ops_maint 是环境、部署、健康采集和保留清理的唯一入口。Worker 与工具从固定受信控制提交启动；源码和控制升级经 Gitee 到达 CI 主机。依赖源、镜像摘要和完整 LLVM revision 必须配置为本地可达的固定版本。

每任务一个 Rootless Docker 容器，Agent、构建、安装与测试共用 identities.task 非 root UID 和 identities.gid。candidate/base/experiments 是数据版本。原生命令与内置工具共用 candidate checkout，不维护独立探索副本。控制程序可用容器命名空间 UID 0 执行固定文件准备操作；不按 UID 扫描、取消或清理任务进程。

可写挂载只有 work/<task>/<run> → /task 和 runs/<task>/<run>/artifacts → /task/artifacts。RPC socket 放在 work 的 rpc 子目录。控制快照与版本化依赖只读挂载。宿主 state、commands、logs、sealed 不挂载。单命令由执行服务取消对应进程组；任务停止才停整个容器。宿主 artifacts 从开始执行即持久化；清容器不删除证据。

配置参考 config.example.json 与 profiles/config.template.json。保留 B 的镜像 digest、依赖 tree/hash、控制快照、实际工具验证与 cgroup probe。rotate.py 仅在依赖/配方改变时构建和验证候选环境；复用已验证配方，不安排每日强制重建。失败不切换活动镜像。镜像验证阶段分别执行 build/install/smoke，所需 profile 必须在真实工具链上验收。

`branch_profiles` 将实际任务目标分支映射到 `profiles` 的键。模板中的 `CI_dev → triton_v3.0` 是显式示例；`triton_v3.0/3.3/3.6` 同名目标直接选择对应 profile。当前 Gateway 还监听 `main` 的 push，因此启用前必须根据 main 的实际 Triton/LLVM 版本补上其映射；每个允许的 PR 目标分支也须有对应 profile 或显式映射。映射只选择环境，不改变任务 SHA、目标分支或完整 LLVM revision，不能映射被排除的 `CI_dev_forPR`。

后端测试路径默认是 `tests`。示例环境明确写出 `BACKEND_TEST_PATHS: "tests"`，多个路径按 shell 参数拆分；这是后端测试路径，与 `BACKEND_TEST_COMMAND` 的 smoke 命令分别配置。需要 JSON 数组时可在对应 profile 下设置 `"tools": {"backend_test_paths": ["tests"]}`，Worker 优先使用该数组。

以下部署及运维入口均支持 `python3 scripts/local_ci/ops_maint/<入口>.py --help`，查看帮助不读取实际部署配置：`preflight`、`rotate`、`install`、`health`、`watchdog`、`retention`。

部署顺序：

1. 填写真实 Gitee URL、镜像 digest、依赖位置与模型凭据来源。配置模板不表示机器已部署。
2. python3 scripts/local_ci/ops_maint/preflight.py --config <配置> --configuration-only
3. python3 scripts/local_ci/ops_maint/rotate.py --config <配置> --profile <名称>
4. python3 scripts/local_ci/ops_maint/preflight.py --config <配置> --probe-runtime
5. 用 `python3 scripts/local_ci/ops_maint/install.py --config <配置> --credentials-env <凭据文件> --render-dir <目录>` 生成并审查用户级 systemd units；加 `--apply` 安装但不启动服务。凭据 EnvironmentFile 必须为运行用户所有且权限 600。

health 和 watchdog 分别由同机独立 timer 每约五分钟运行；不依赖任务 Agent、Worker poller 或 GitHub 接收服务。采集读取 runs 文件状态，不使用数据库。公开数据在上传前按字段白名单构造，不包含模型配置、路径、挂载源、凭据或原始异常。snapshot/<worker-id> 与 watchdog 使用无父快照提交和旧 SHA 条件更新；main 页和配置保持正常历史。Gitee 不可读显示 unknown，保留已有异常；可读快照超过 20 分钟显示 stale。watchdog 仅保留最近 100 条异常/恢复。整机停机时同机 watchdog 也停止。

retention.py 默认预览，--apply 执行。已 published 且超过 30 天的证据可清理，必要 pending 始终保护；延长调查可在 run state 配置 retention_until。原始结果、命令索引与到期摘要保留。超出预算或磁盘不足时返回 pause_intake，由 Worker 停止接单。远端 Release 附件到期时先发布 expired delivery-index，再按已保存的附件 ID 查询并删除；重试只删除仍存在的对象。仍须在目标仓库实测权限、下载、删除与配额。本地清理不压缩 Git 历史。
