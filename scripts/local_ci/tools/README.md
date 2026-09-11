# Local CI tools

工具可由 shell、Python 或 AI 调用。Codex 根据 PR 意图、实际改动和项目最低规则，
自主组织构建、测试与审查；工具负责执行具体工作，不负责调度 Codex。

| 工具 | 准备条件 | 行为 |
| --- | --- | --- |
| environment | 已准备的运行环境 | 检查命令、Python 依赖、LLVM 与 pip 依赖一致性 |
| control_plane | 被测 checkout 与 base SHA | 检查改动文件并运行相关控制面回归 |
| frontend_build | 前端构建依赖 | 构建 frontend wheel |
| frontend_install | frontend wheel | 无依赖更新地安装 wheel，验证实际 import 来源 |
| frontend_tests / frontend_smoke | 已安装 frontend | 选测 / 前端 smoke，可独立调用 |
| backend_build | 后端构建依赖 | 独立构建 backend wheel |
| backend_install | backend wheel、frontend | 安装 wheel 并验证后端发现 |
| backend_tests / backend_smoke | 已安装 frontend、backend | 后端选测 / 真实 JIT |
| flaggems | 可用后端与预置 FlagGems | 按影响或全量清单逐算子测试 |
| compile_time / pass_profile / ir_serialization | 可用后端 | 正确性及有效测量检查，再与同条件基线比较 |

准备条件用于安排工作，工具不会自动递归执行构建、安装或其他阶段。
编译可调整 `jobs`，并选择 `fresh` 或 `incremental`；共享环境依赖由 `prepare/` 管理，
生产源码链路经 Gitee。FlagGems 使用服务器预置目录，测试缓存与日志写入任务产物目录。

## 调用

```bash
python3 /opt/local-ci/control/scripts/local_ci/tools/basic_tools/runner.py frontend_build \
  --context /task/artifacts/candidate-context.json --parameters '{"jobs":2,"build_mode":"incremental"}' --execute

python3 /opt/local-ci/control/scripts/local_ci/tools/basic_tools/runner.py frontend_tests \
  --context /task/artifacts/candidate-context.json --parameters '{"paths":["tests/test_unit.py::test_add"]}' --execute
```

`context` 中 `source_dir`、`artifact_dir` 必填；`target_sha` 默认读取 checkout HEAD。
`python_bin`、`profile`、`base_sha` 等指定环境和比较对象。profile 的 tools 配置包含
`env_scripts`、`backend_env_scripts`、`llvm_dir`、`backend_dir`、`backend_test_paths`、
`backend_smoke_argv`、`expected_backend` 与 `flaggems_dir`。

不带 `--execute` 输出命令计划；Python 调用 `plan(tool_id, context, parameters)` 或
`execute(tool_id, context, parameters)` 使用同一实现。执行结果写到
`artifact_dir/<tool_id>/result.json`，命令输出在同目录的 `command.log`。
工具报告记录状态、参数、耗时和产物，可用于 AI 最终的 `agent-result.json` 汇总。

build 参数为 `jobs`、`build_mode`；install 可用 `wheel` 指定现有 wheel，
默认读取 `artifact_dir/<build_tool>/wheel.json`。若构建产物在另一目录，
可用 `dependency_artifacts[build_tool]` 指定该目录。
pytest 参数为 `paths`（相对路径或 node ID）、`keyword`；FlagGems 为
`mode`（impact/full）、`ops`、`categories`；性能参数为 `kernels`、`repeat`、`warmup`。

普通 pytest 输出简单计数，失败、空收集和全部跳过都不会显示为通过。
性能基线由 `performance_baselines[tool_id]` 指定文件、提交与环境信息；比较时核对
后端、算子和采样条件。没有可比基线时报告 `not_comparable`；有效性能回退只报告，
测量无效或正确性失败则返回失败。

## 审查与补充验证

`ai_review_tools/` 提供架构及专项审查说明。`ai_custom_tools/` 说明任务内脚本的用途。
Codex 可以直接运行已有测试、编写定向复现或使用其他命令，在最低检查范围内自主选择
顺序并补充验证。最终摘要列出已完成检查、审查结论、未完成项与所选重要证据。
