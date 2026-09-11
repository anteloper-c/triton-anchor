# Local CI 当前架构

统一入口与运行模型见 [Local CI](../scripts/local_ci/README.md)，合并来源与验收边界见 [合并记录](local_ci_merge.md)，环境、健康、配置和部署见 [ops_maint](../scripts/local_ci/ops_maint/README.md)。

当前版本采用直接 AI_CI_PROGRAM 入口、单任务非 root 用户、A runner 的唯一工具接口、文件进度与封存后独立交付。此前 Skill、四 UID、SQLite 和邮件方案已由本轮合并设计替代；历史实现可从 Git 提交查阅，不再作为部署指引。
