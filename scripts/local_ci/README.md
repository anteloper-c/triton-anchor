# Local CI 脚本

本目录是确定性 Local CI、Codex AI 审查和结果发布链路的可信控制面。`LOCAL_CI_SCRIPT_DIR` 必须指向本目录，确保每次任务创建的 runner 快照包含完整模块树。

## 模块划分

| 模块/入口 | 职责 |
| --- | --- |
| `poll_gitee_and_run.sh` | 服务器稳定入口，轮询 Gitee task ref、创建可信 runner 快照并编排每次任务。 |
| `orchestration/` | 提供 metadata 获取和确定性 CI 容器调度。 |
| `deterministic_ci/` | 执行构建、smoke 测试、FlagGems 测试并收集性能证据。 |
| `codex_ai/` | 执行非阻塞 Codex 审查、校验结构化报告并维护 prompt 契约。 |
| `results/` | 向 Gitee 发布产物，并将已完成的结果回写到 GitHub。 |
| `shared/` | 提供跨模块共享且需要保持稳定的任务和结果路径协议。 |

模块依赖方向如下：

```text
poll_gitee_and_run.sh -> orchestration
                      -> codex_ai
                      -> results

orchestration -> deterministic_ci

codex_ai -> shared
results  -> shared
dashboard -> shared
```

确定性 CI 必须先于 Codex AI 执行。Codex 只提供辅助审查，不能改变确定性 CI 的退出码。任务结果成功发布后，才能将对应 SHA 标记为已处理。

## 服务器入口

`scripts/local_ci/poll_gitee_and_run.sh` 是唯一保留在根目录的可执行脚本，也是 systemd/cron 和人工运行 poller 的稳定入口。它包含 poller 完整实现，并调用分层目录中的 canonical 模块；其他脚本不提供根目录兼容入口。

本次迁移调整源码布局、workflow 引用和部署文档；task ref、结果路径、产物名称、报告 schema、status context 和性能缓存路径均保持不变。
