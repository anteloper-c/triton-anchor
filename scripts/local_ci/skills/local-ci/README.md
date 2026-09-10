# Local CI Skill 维护说明

`SKILL.md` 是 CI 运行期唯一入口。Harness 读取可信控制版本中的该文件，再按其本地 Markdown 链接顺序完整加载四个 references；不会依赖 CLI 自动发现，也不会递归读取其他 Markdown。本 README 面向维护者，不参与任务提示词加载。

原 `scripts/local_ci/ai_ci_program.md`、`architecture_review.md`、`ai_review.md` 分别迁入 `references/AI_CI_PROGRAM.md`、`references/architecture_review.md`、`references/ai_review.md`；旧文件删除，避免维护多个入口。`references/project_conventions.md` 补充实际 README、API 契约、编译配置与 Python 代码中的项目事实。

修改应保持顶层设计与后续确认的调整：GitHub 前置检查串行、每任务单 Codex、每个 PR attempt 使用独立容器、`impact/v5` 必检不可减免且推荐项按具体风险选择、Triton 3.0 的后端性能能力边界，以及仅服务器使用公司现有模型配置。Codex 在容器内以 `danger-full-access` 和 `approval_policy=never` 运行，可以通过原生命令和编辑探索独立源码/venv；正式检查及阻断复现仍经 MCP/Harness 核验。原生事件与源码快照是宿主私有调查记录，不能代替正式通过证据。业务决定仅为 `continue/block`；`submit_review` 和检查状态保留。回执及 Codex 发布恢复入口已删除。

架构规则原文、冻结源码位置、高风险问题的两次 candidate 失败和 base 通过证据，以及恢复时复用已完成检查的要求均保留。Skill 内容由可信控制版本固定；候选 PR 不得替换运行中的 Skill。更新后检查入口 frontmatter、四个链接及顺序，并使用 skill-creator 的 `quick_validate.py` 校验结构。

`agent_ci/skill.py` 只读取 `SKILL.md` 正文中 `references/` 下的简单相对 Markdown 链接，按声明顺序加载；文件须为非空 UTF-8，不能经过符号链接或越出 Skill 目录。入口只放资源链接，项目代码定位放在 references 中，不递归加载。加载错误会阻止模型启动，不尝试旧入口。

`agent_ci/codex.py` 把入口及依赖的完整内容交给 `codex exec`，不依赖自动发现，也不调用 Skills 上传 API。会话内 `TASK_SKILL.md` 是 Harness 生成的快照，以 `0400` 交给 Codex UID；文件所有者仍可更改其权限，这不是保护可信规则的边界。可信来源是宿主机冻结控制版本，任务目录的 `skill-manifest.json` 记录实际加载文件与 SHA256。会话恢复校验 Skill 摘要，禁止同一会话混用两版规则；不会从 Codex 修改过的会话副本加载规则。部署更新前先处理在途任务，按新控制版本准备并验证镜像，新任务使用新版 Skill。

| 业务决定 | 既有执行与交付事实 |
|---|---|
| `continue` | 在能力和预算内继续取证、运行检查、审查或恢复未完成验证；成功封存后 Codex 结束。 |
| `block` | 已确认代码/架构阻塞，或必要证据、环境、模型在有限恢复后仍不足；保留具体原因，不伪造成功。 |

通过、失败、环境异常是结果分类；取消、排队和上传是生命周期。Harness 验证证据，封存后独立上传 Gitee；成功上传即本地交付完成。GitHub 独立发布，PR 状态仍先于 Dashboard 更新；GitHub 发布失败由 Actions 展示并重试。没有模型自报通过的接口，也没有 GitHub → Gitee 回执。

本地目录格式依据 [OpenAI Skills 文档](https://learn.chatgpt.com/docs/build-skills)。这里由 Harness 显式加载可信包；维护时无需将公司 CI Skill 上传到模型平台。
