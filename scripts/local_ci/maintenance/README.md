# 运行维护

`maintenance/` 负责健康采集、异常观察和本地结果保留。服务由 `prepare/install.py --apply` 一并安装启动。

- `health.py` 每约五分钟独立采集 Worker 心跳、任务状态、容器资源和磁盘情况，将公开快照提交到 Gitee 健康仓库。
- `watchdog.py` 由同机独立 timer 读取公开快照，记录异常及恢复。Gitee 不可读时显示 unknown，超过配置时限的快照显示 stale；记录保留最近 100 条。
- `retention.py` 每天清理已发布满 30 天的本地大日志和证据，保留结果摘要。准备中、运行中和待上传任务不清理；`retention_until` 可延长指定 run 的本地保留期。

公开快照按字段构造，包含状态、时间和资源统计。宿主路径、模型配置、凭据与原始异常留在本机。health 与 watchdog 不依赖 Worker 或 Codex 运行；同机停机时二者也会停止。

结果和选择上传的文件保存在 Gitee 的 `runs/<task_id>/<run_id>/`。本地清理不改写远端结果。磁盘可用空间不足或结果存储超出配置预算时，Worker 暂停接收新任务。

可单独运行 `python3 scripts/local_ci/maintenance/<入口>.py --config <配置>`。health、watchdog 加 `--publish` 发布快照；retention 默认预览，加 `--apply` 执行清理。各入口均支持 `--help`。
