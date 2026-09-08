# CI 指南

`main` 只保留必要的事件入口和调度，完整 CI 实现及维护说明统一保存在 `ci_repo`。

请阅读 [ci_repo 分支的 CI 使用与维护指南](https://github.com/anteloper-c/triton-anchor/blob/ci_repo/docs/ci_guide_zh.md)，了解前置检查、外部贡献者审批、Codex 自主验证、常驻环境、结果回写和故障恢复。

修改 CI 控制程序时使用 `ci_repo`；不要根据 `main` 的目录布局查找服务器实现。

纯文档变更免于构建，但仍需完成 PR 信息与架构审查；GitHub 最终检查以当前 PR 对应的受信验证结果为准。
