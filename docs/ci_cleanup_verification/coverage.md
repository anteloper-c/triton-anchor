# 常驻环境收尾与任务目录回收验收

汇总和执行命令见 [verification.md](verification.md)、[verification.json](verification.json)。本轮基于本地 CI_dev `8fe195545b50b16f7c420741b30c1e112da78f63`；main 没有修改。

| 要求 | 实现 | 验证证据 |
| --- | --- | --- |
| 成功任务释放 venv、源码及缓存，保留上传证据 | `agent_ci/workspaces.py`、Worker 收尾、独立 outbox | `test_workspaces.py`：上传前删除目录、上传失败不重建、不重复模型调用、已封存证据不多复制一份 |
| 失败任务有期限和容量预算，活动任务受保护 | 默认保留 24 小时、逻辑预算 100 GiB；只回收非活动任务 | 期限到期、容量驱逐、活动目录超过预算仍不删除、磁盘不足时继续上传 |
| 进程结束可验证，清空标记也不能遗留运行进程 | 非 root 专用 UID、不可撤销 no_new_privs、root pidfd reaper | `test_executor.py`：真实 Linux 子进程、setsid、空环境、旧 Worker 遗留、PID1/root 保护、回收失败、no_new_privs |
| 环境异常隔离，后端能力不降级 | `manager.py`：dirty、共享内容/权限/属主指纹、公共目录基线、真实设备命令、隔离停止确认 | `test_manager.py`：公共残留、依赖内容与模式变更、设备命令失败、超时、停止未确认阻止接单/轮换/回滚、隔离时间与 GC |
| 回收不丢日志，不复用已删除安装状态 | 优先引用已封存证据；未封存日志先归档并 fsync；记录失效标记与代际绑定 | 回收后续跑重建、回收前有效记录复用、同配方换代不复用、证据保存前不释放 lease、断电时未写完整 execution 的日志保留 |
| 重启、取消与清理中断恢复 | 原代际回收进程、登记旧已完成目录、租约恢复、清理状态重放 | Gitee 离线仍回收进程、旧任务失效、无 lease 遗留目录、清理中断、符号链接和嵌套挂载保护 |
| 环境验证纳入最终封存 | `Supervisor.before_seal`、封存阶段禁新执行、可信 finish 等待预算 | 清洁检查失败无整体绿色、封存写盘失败后不能再启动测试、MCP/Codex 时限覆盖环境检查 |
| 维护故障可见且告警有闭环 | workspace 健康、代际状态、watchdog、现有 v4 Dashboard | `test_deployment.py`、`test_watchdog.py`：配置缺失预检、异常去重、发送失败重试、恢复通知、健康字段缺失不误恢复、摘要不带主机路径/凭据 |
| GitHub 发布和权限边界保持约定 | GitHub workflow、main 路由、status → comment → Pages 顺序未改 | 原网关、Skill/MCP、单向发布和 Gitee 保留周期回归仍运行 |

目录预算只涵盖任务工作区的逻辑字节；不会删除 outbox 或持久证据来达到预算。健康报告另外记录持久证据大小及 state 文件系统剩余空间，空间不足阻止新构建，保留上传重试。公共目录中的未知残留按异常隔离，不盲删。既有外部容器无法确认管理器所有权时，不会强行停止，保持阻塞供运维处理。

真实执行的是 Python 控制逻辑、SQLite、MCP/Unix socket、文件回收、Linux 权限与进程回收、本地 bare Git 和邮件 outbox。Docker 命令、依赖/设备探测、模型、GitHub/Pages 和 SMTP 网络使用边界替身。未执行真实 Docker/LLVM/后端编译、厂商设备清洁验证、公司模型调用、实际邮件、服务器部署或线上 GitHub/Gitee 操作。
