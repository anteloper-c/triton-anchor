# Local CI v4 开发约定

当前架构和运行入口见 README.md；最高指导是 new_CI.md 及用户确认的实施计划。旧固定流水线、一次性 Codex 容器与“AI 永远非阻塞”的说明已被替换。

- agent_ci：任务身份、可信最低策略、持久 journal、MCP 服务、Docker 执行器、Codex 会话与 Gitee 单向结果发布。
- tools：一个入口对应一个真实检查，不能转调旧整条 runner。命令失败、空测例、缺必要数据必须准确反映在退出码中。
- environments：可信版本配方、LLVM provenance、镜像发布、任务容器 attempt、lease、轮换与回退。
- deploy/maintenance：部署预检、systemd、独立健康发布、外部 watchdog 与 SMTP outbox。
- GitHub：CI_dev 的 gateway_v4 负责 PR 信息检查、顺序前置检查、审批、投递、取消、结果接收和页面发布；main 保持小路由。

任务与结果的身份必须一致。变更任意 SHA、PR 元数据或执行策略须有明确新任务/新执行身份。结果是否通过由真实执行记录与必检检查计算，不能把模型 summary 当作退出状态。

PR 路径先识别控制程序与运行规则，再识别纯文档。重命名、删除、symlink、gitlink 和 vendored Triton 都参与分类。新增工具应声明依赖、能力、参数限制、失败语义、产物，以及相应行为测试。

架构阻断需具体可信规则和原文/代码证据；额外高风险问题需同环境同脚本的重复 candidate 失败和 base 通过。source_only Python 复现禁用 site packages；运行时复现先验证对应版本安装，防止导入 seed wheel。

测试使用真实控制逻辑，仅替换外部边界。不得把模拟编译或模拟模型结果标为真实测试通过。修改阶段依赖、身份、恢复、发布或轮换时，应增加能够发现行为回归的测试，而不是只断言脚本包含某个字符串。

受管目录操作必须验证绝对路径和边界。候选代码及其输出均不可信；不能读取其配置来获得宿主机、Docker、模型或发布权限。生成脚本仅在任务目录使用，改进建议只作为证据供维护者采纳。
