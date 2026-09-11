# Local CI tools

工具以 ci_repo 的三类目录为基础，统一通过 `basic_tools/runner.py` 的 `plan(tool_id, context, parameters)` 生成计划。
`actions.py` 共用构建、安装、真实 import 检查与报告处理；`evidence.py` 对内置工具与原生命令使用同一套报告判据。
执行服务记录实际命令和执行身份，MCP 与 CLI 只负责调用，不维护第二份工具或依赖表。

| 工具 | 依赖 | 行为 |
| --- | --- | --- |
| environment | 无 | 检查依赖、完整 LLVM revision 与环境指纹 |
| control_plane | 无 | 冻结 diff 的语法/格式检查，代码控制面改动运行实际回归测试 |
| frontend_build | environment | 构建 wheel，记录摘要和被测提交 |
| frontend_install | frontend_build | 安装 wheel 并核对实际 import 来源及文件摘要 |
| frontend_tests / frontend_smoke | frontend_install | pytest 选测 / 前端 smoke，相互独立 |
| backend_build | environment | 独立后端 wheel 构建 |
| backend_install | backend_build、frontend_install | 安装并验证后端发现 |
| backend_tests / backend_smoke | frontend_install、backend_install | 后端 pytest / 真实 JIT smoke |
| flaggems | backend_smoke | 按影响或全量清单执行算子，保留逐算子证据 |
| compile_time / pass_profile / ir_serialization | backend_smoke | 正确性及有效测量检查，再按同条件基线比较 |

后端适用性来自 profile 声明。声明支持却缺少工具链是环境错误。
`backend_build_requires_frontend` 仅供确实需要候选前端的配方增加依赖。
每次执行使用独立产物目录，`context.dependency_artifacts` 指向实际依赖执行的产物；重建不覆盖历史。
测试或 smoke 前重新核对已安装模块的实际文件摘要。

## 调用

```python
from tools.basic_tools.runner import plan

spec = plan(
    "frontend_tests",
    context,
    {"paths": ["tests/test_unit.py::test_add"], "keyword": "not slow"},
)
# spec.commands: [{argv, cwd, env, timeout}], spec.dependencies, spec.artifacts
```

`context` 必须包含 `source_dir`、`artifact_dir`、`task_id`、`target_sha`、`triton_version`；
可包含 `python_bin`、`trusted_python_bin`、`tools_dir`、`task_venv`、`environment_fingerprint`、
`task_root`、`base_sha`、`dependency_artifacts` 和 `completed_tools`。
产物写入 `artifact_dir/<tool_id>`，跨阶段产物从 `dependency_artifacts[tool_id]` 获取。

```bash
python3 /opt/control/scripts/local_ci/tools/basic_tools/runner.py frontend_build \
  --context /task/context.json --parameters '{"jobs":2,"build_mode":"incremental"}' --execute
```

不带 `--execute` 仅输出计划。CLI 与原生命令均须由任务执行服务记录，才能关联正式结果。
每任务一个容器、一个非 root 用户；各类工具共享同一执行身份和预算。

`profile.tools` 配置 `llvm_dir`、`required_commands`、`required_modules`、可选 `required_source_files`、
`env`、`env_scripts: [{path,args}]`；后端另配 `backend_dir`、`backend_wheel_pattern`、
`expected_backend`、`backend_test_paths`、`backend_smoke_argv`、`backend_env_scripts`、`flaggems_dir`。
源码、子模块及额外依赖来源由任务外 ops_maint 配置，生产链路经 Gitee。

build 参数：`jobs`（1–64）、`build_mode`（fresh/incremental）。
pytest 参数：`paths`（相对测试路径或 node ID）、`keyword`。
FlagGems 参数：`mode`（impact/full）、`ops`、`categories`；full 策略要求完整算子清单。
性能参数：`kernels`、`repeat`、`warmup`。基线由 `performance_baselines[tool_id]` 提供
`{path,sha256,base_sha,profile_id,llvm_revision,environment_fingerprint}`，并核对后端、算子及采样条件。
无基线或条件不一致记录 `not_comparable`；有效性能回退仅报告，测量无效或执行失败阻塞。

JUnit 缺失、没有实际用例、全部跳过或存在错误都不通过。
`evidence.evaluate(tool_id, artifact_dir, exit_code, context, parameters)` 返回 status/details/subject/scope；
原生命令可在 `parameters.reports` 指定产物目录内的报告名。报告不能单靠自报覆盖或退出 0 抵扣必检。

`ai_review_tools/` 保存架构和专项审查要求；`ai_custom_tools/` 用于任务内复现、选测与分析。
不维持独立 deterministic_ci 目录、旧工具 ID 或第二套执行入口。
