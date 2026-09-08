# Local CI Skill 迁移验收

依据：2026-09-08 用户提供的 `AI_LOCAL_CI_PRODUCT_PLAN_SKILL_HARNESS.md`、文件对照附件及本轮明确要求。目录采用本轮给出的最终结构；继续使用现有 CLI、Harness、MCP、业务工具、Gitee 与结果协议。

| 要求 | 本轮实现 | 验证证据 |
|---|---|---|
| 唯一 Skill 入口 | `skills/local-ci/SKILL.md`，四份 references；删除旧三份平铺提示词，`tools/` 保持原位 | `test_skill.py` 校验实际入口、引用顺序、旧入口不存在；Skill Creator 结构校验通过 |
| Harness 显式加载 | `agent_ci/skill.py` 解析入口链接；`codex.py` 传入完整可信包，保存 `TASK_SKILL.md` 和 `skill-manifest.json` | `test_codex.py` 验证初始/恢复 stdin 内容、只读快照、文件摘要与会话身份；缺文件或摘要变化不启动模型 |
| 有效引用与可信版本 | 缺失、空文件、越界、符号链接、重复引用失败关闭；恢复绑定相同任务、provider 与 Skill 摘要 | `test_skill.py` 路径和缺失资源测试，`test_codex.py` 拒绝旧平铺会话/不同 Skill，`test_deployment.py` 部署预检 |
| 单任务单 Codex，继续/阻塞 | Skill 明确两个业务决定；检查和任务状态保持原协议；允许 Harness 串行恢复 | `test_codex.py` 验证禁用 multi_agent，`test_skill_flow.py` 验证中断后单实例继续；三个代表场景人工逻辑推演 |
| 真实工具调用及 PR 闭环 | 真实 Worker、MCP stdio、Unix socket、Supervisor、DockerExecutor、run_tool、证据封存与收发回执 | `test_skill_flow.py` 三项：成功后等待匹配回执；真实文档冲突失败；工具通过但 PR 审查失败。错误回执拒收，已通过 environment 不重跑 |
| 保留已有前置和版本范围 | main 路由及 CI_dev 工作流不变，Basic → API → Security；3.0 后端性能能力、其他版本前端能力保持 | 现有 gateway、policy、环境及历史契约回归；新 Skill 路径分类仍要求控制面契约检查 |
| 补齐已披露 MCP 参数漏洞 | MCP/RPC 同一参数校验；公开 start_check 不接受 custom，执行器禁止基础工具身份搭配自定义脚本 | `test_mcp_boundary.py` 九项：非法请求无执行记录、私有入口不可调用、合法自定义独立记账、历史冒充记录不满足必检 |

## 模拟边界

模型边界由单个脚本化 Codex peer 替代，未调用公司模型 API。Docker 边界使用既有 fixture，提供测试 UID/venv、虚构 LLVM 与包探测结果；`tools/run_tool.py` 和 `contract_checks.py` 实际执行，文档冲突由真实 Git 检查产生失败，没有伪造该工具的通过/失败结果。

Gitee 使用临时本地 bare Git 仓库；GitHub API 是记录式 fixture，Pages 发布边界标记成功后运行真实回执代码。测试证明本地控制及证据链路行为，不证明模型真实遵循 Skill，也不代表公司环境、编译器或线上服务验收。

仍未进行：真实 Codex/provider 联调、LLVM/厂商后端编译、GPU/CModel/FlagGems/性能硬件测试、服务器部署、实际邮件、线上 GitHub/Gitee/Pages 操作。本轮未推送任何远端，main 未修改。

完整回归结果和各套日志见 [verification.md](verification.md) 与 [verification.json](verification.json)。旧的 [ci_v4_verification](../ci_v4_verification/verification.md) 保留为此前 421 项验收记录。

## 重现

在具有项目测试依赖的 Linux Python 环境执行：

```bash
python3 scripts/local_ci/agent_ci/verify.py --output-dir /tmp/triton-anchor-ci-skill-verification
```

本机使用已有 `/tmp/triton-anchor-ci-v4-deps` 中的 pytest/PyYAML/jsonschema/tomli；没有调整公司模型配置或服务器环境。升级和回滚的 Skill/会话兼容条件见 [部署说明](../../scripts/local_ci/deploy/README.md)。
