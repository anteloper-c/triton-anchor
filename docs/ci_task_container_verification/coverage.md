# PR 独立任务容器验收覆盖

本轮以 CI_dev `189284df8827fae3aec6d2476730baae6bebf572` 为基线，按用户后来明确选择的方案③实施。每 PR 独立任务容器替代此前常驻测试容器；这项用户确认优先于原 new_CI.md 的常驻容器要求。main 保持 `4fc20aecb82113ca94c2043710cc6970b222eedf`。

| 要求 | 实现与验证证据 |
| --- | --- |
| 普通 CI 用户管理，Rootless Docker | environments/runtime.py 固定 endpoint、校验 rootless daemon/cgroup v2/systemd、资源配置和对象标签；deploy/runtime_probe.py 实测 cgroup 配额并绑定安装证明。test_rootless_manager.py、test_rootless_deployment.py。 |
| 可信镜像与精确 LLVM | Dockerfile/container_fs.py 构建可信控制代码及校验过摘要的依赖；版本配置和 LLVM 精确提交决定镜像身份。镜像验收失败不切换新任务，PR LLVM 候选不直接替换正式镜像。test_rootless_manager.py。 |
| 每 PR 独立安装、数据与四身份 | 每个 attempt 独立容器/数据卷/会话卷，candidate/base/diagnostic/Codex 四个非 root UID。镜像只读，不挂 Docker socket、宿主 state 或发布凭据。test_container_fs.py 运行生产 helper 的真实文件与 UID 操作；test_executor.py 检查隔离 venv、权限和进程回收。 |
| Codex 同容器，通过 MCP 工作 | codex.py 显式加载唯一 Skill，将公司认证经 stdin 写入私有会话卷；禁用 native shell/多 Agent 等，MCP 绑定当前任务。test_codex.py、test_mcp_boundary.py、test_skill_flow.py。 |
| 保留诊断便利性 | diagnostic 只读正式 candidate/base，reproduction 保留归因依赖，experiment 有独立可写源码和 venv；生成内容、命令、退出码与身份可追溯，不能代替最低基础检查。test_executor.py、test_agent_ci.py、test_skill_flow.py。 |
| 保留工具与能力 | 十个确定性业务工具继续独立调用；3.0 必须有后端与性能能力，其他版本仅前端。依赖、wheel、空测试、基线及性能错误分类保持回归。tools/tests、agent_ci/tests、environments/tests。 |
| 重启、取消和清理 | 同 attempt 恢复有效阶段；换 attempt 失效旧安装结论及 CLI 会话指针。真实 pidfd 回收角色进程，清理测试时保留 Codex 接收 finish；失败卷按 TTL/预算保留。中断的卷内产物补导出，无法取证时禁止删除；卷确已丢失明确记 infra 事实。test_executor.py、test_workspaces.py、test_rootless_manager.py。 |
| 单向结果及上传恢复 | Codex 封存即结束，Harness 只上传固定 outbox；Docker 不可用仍尝试上传。封存后 socket 清理异常不改写不可变结果。test_workspaces.py、test_agent_ci.py、既有 GitHub receiver 回归。 |
| 用户级部署与迁移 | install.py 生成/安装/回退 user units，自动任务不 sudo；migrate_state.py 只读旧快照，复制终态/outbox/证据，拒绝未排空、摘要或目标冲突，不继承旧运行环境通过记录。deploy/tests、test_migrate_state.py。 |
| GitHub 行为保留 | 无 workflow 或 main 变更，门禁仍 Basic→API→Security，外部 fork 审批及 SHA 过期防护不变，仍 status→comment→Pages，无 Gitee 回执。scripts/ci/tests 与既有完整回归。 |

机器可读总结果和每套日志由 agent_ci/verify.py 生成，见 [总报告](verification.md)。生产 helper、真实本地 Git、SQLite、Linux 子进程和 UID 权限得到实际执行；Docker 命令、模型响应、远端 HTTP/Pages/SMTP 边界使用 fixtures。

未执行真实 Docker daemon/命名空间/Rootless 配额、公司模型、LLVM 编译、厂商 PPL/后端、仿真性能、邮件、服务器部署或线上 GitHub/Gitee 验收。特别是 Rootless 实际兼容性与配额证明，需部署时运行显式 --probe-runtime，再进行 3.0 正常 PR 与故意失败 PR 验收。不能把本报告作为服务器已验证通过的证据。

本轮未推送远端；没有操作 CI_dev_forPR，也没有对 RACE-org/triton-anchor 执行远端操作。历史常驻容器报告保留作为此前版本记录。
