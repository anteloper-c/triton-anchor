# Local CI 开发约定

当前架构与运行方式统一见 [README](README.md)。

- `agent_ci` 管理一个任务的生命周期、执行记录、原生命令证据与封存；`state.py` 是文件进度的单一写入入口。
- `tools/basic_tools/runner.py` 定义工具 ID、参数、真实依赖和 plan；CLI 与 MCP 共用它，报告判据在同目录 `evidence.py`。
- `tools/ai_review_tools` 保存架构及专项审查说明；`ai_custom_tools` 说明临时分析用途，生成文件写任务 artifacts。
- `ops_maint` 保存精确依赖、Rootless 容器、配置、预检、部署、健康与保留；不引入第二套常驻运行框架。
- `schemas`、`agent_ci/protocol.py` 和 `scripts/ci/gateway_v4.py` 共用身份、最低范围、执行/工件与交付判据。

先验证具体行为再扩大回归：测试选取、失败归类、重装失效、取消、重启与发布需覆盖实际边界。不要用断言实现细节的测试维持已被设计删除的四 UID、Skill 层级或 SQLite 表结构。新结果只有一份当前格式，旧结果只作历史查看。

原生命令正式选测可直接执行 `tools/basic_tools/pytest_exec.py --installation <已记录的 installation.json> --import-report <产物目录>/import-origin.json -- <pytest 参数>`，同时输出 JUnit，并通过 `record_check` 关联已观察到的执行。判据比较实际测试进程的 import 来源，不能仅声明覆盖某个 tool ID。

代码提交不代表生产验收；服务器模型/LLVM/后端、Rootless 限额、Gitee 附件权限与 required checks 都需要真实目标环境验证。配置示例不含真实凭据，不自动启动服务。
