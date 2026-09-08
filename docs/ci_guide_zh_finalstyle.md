# Local CI v4 文档入口

此页面原先描述已移除的 Local CI 架构。旧操作命令与配置不再适用，完整历史可从 Git 历史查阅。

当前版本使用常驻容器、Codex 自主调度和主机证据 broker。请使用以下维护中的文档：

* [Local CI 工作流程与配置](../scripts/local_ci/README.md)
* [主机配置示例](../scripts/local_ci/config.example.json)
* [持久服务、每日维护与离线监控部署](../scripts/local_ci/deploy/README.md)
* [工具、AI 审查与任务内辅助脚本](../scripts/local_ci/tools/README.md)
* [控制面开发与测试指南](../scripts/local_ci/DEVELOPMENT_GUIDE.md)

只有 Triton 3.0 提供后端、算子与性能检查；其他版本仅执行适用的前端检查。GitHub 的 v4 router/worker、实际受信部署及分支保护规则须配套启用。仓库示例和本机合成 fixture 测试不代表真实编译器/后端或远端服务已验收。
