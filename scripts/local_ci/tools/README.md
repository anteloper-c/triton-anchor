# Local CI tools

Codex 通过运行时客户端向证据 broker 请求工具。控制器选择并只读挂载此目录；
被测 PR 中的同名脚本不作为控制程序加载。工具不规定整体顺序，依赖只表达真正的
构建产物要求。最低检查范围由运行时策略决定，不能由 agent 参数降低。

`basic_tools/runner.py` 定义各工具的命令与依赖，`actions.py` 实现容器内的
产物校验、安装和结果记录。前后端共用阶段实现，通过独立工具 ID 区分；
不会为每个阶段再创建一层脚本。前端源码来自本次被测 checkout，后端源码来自受信配置的 `backend_dir`。

| 工具 ID | 实际行为 | 必须已有的本任务成功检查 |
| --- | --- | --- |
| `environment` | 检查命令、Python 模块、源码、LLVM 目录，执行 `pip check` | 无 |
| `frontend_build` | 清理有界构建输出，PEP 517 wheel 构建，记录 wheel 哈希及提交 | environment |
| `frontend_install` | 校验 wheel 身份，安装 wheel，隔离 Python 搜索路径后 import | frontend_build |
| `frontend_tests` | 对已安装 wheel 运行选定 pytest 套件或具体用例，输出 JUnit 与计数 | frontend_install |
| `frontend_smoke` | 对已安装 wheel 执行仓库 `tests/test_smoke.py` | frontend_install |
| `backend_build` | 仅在已准备的后端 checkout 构建 wheel，记录身份与哈希 | environment |
| `backend_install` | 仅安装本任务后端 wheel，验证 Triton 后端发现 | backend_build、frontend_install |
| `backend_tests` | 对已安装前后端运行受信 profile 选择的真实测试套件 | frontend_install、backend_install |
| `backend_smoke` | 后端发现及受信 profile 指定的真实 JIT 测试命令 | frontend_install、backend_install |
| `flaggems` | 按影响选择算子，逐算子 pytest，保留超时、日志、CSV/JSON | backend_smoke |
| `compile_time` | 冷缓存独立 worker 编译计时、正确性检查、基线比较 | backend_smoke |
| `pass_profile` | 真实编译的 pass 计时、正确性检查、基线比较 | backend_smoke |
| `ir_serialization` | 真实编译所得 TTIR 的序列化/反序列化及往返测量 | backend_smoke |

从 backend_build 起的八项只适用于 Triton 3.0；其他版本返回 `not_applicable`，明确没有可测后端。
Triton 3.0 的配置或依赖缺失是错误，不能伪装成不适用。基线缺失时只报告候选测量，
并保留 `baseline_available=false`。基准命令失败阻塞；耗时变化只产生报告。

构建、安装、测试套件、smoke 分别调用，不隐式替用户执行下一阶段。两种构建都只
依赖环境检查，便于在前端阻塞时继续定位后端构建问题；后端配方如确实需要候选
前端，可先调用 frontend_install。测试套件与 smoke 互不依赖。
前端打包和 smoke 文件是否存在由对应工具判断；environment 仅在受信 profile
显式配置 required_source_files 时额外要求这些文件，默认不把前端文件问题扩散到后端。
重新构建或安装会使依赖它的成功检查失效，必须重新执行；工具不会复用旧任务 wheel。

## Python API 与独立命令

```python
from tools.basic_tools.runner import plan
spec = plan("frontend_build", controller_context, {"jobs": 2})
# spec: tool_id/status/reason/dependencies/commands/artifacts
# commands: [{argv: [...], cwd: "...", env: {...}, timeout: seconds}, ...]
```

`context` 由控制器构造，至少包含 `source_dir`、`artifact_dir`、`task_id`、
`target_sha`、`triton_version`。`tools_dir` 为受信只读挂载路径；`profile` 为完整
受信环境配置。broker 提供 `completed_tools` 并逐命令记录实际退出码及产物。
计划生成不访问容器文件；checkout、wheel 和工具依赖在容器执行时复核。

生产 `context.python_bin` 指向只读的 `/opt/anchor-ci/runtime/task_python`，
`context.task_venv` 绑定当前任务安装目录。入口跳过任务 `.pth` 与
`sitecustomize`，并让 Python 子进程继续使用该入口。受信预检、wheel 清单与
性能比较使用独立的 `/usr/bin/python3 -I -S`，不能由可写任务 venv 的启动代码
跳过；需导入候选包或运行 pip 时显式调用任务入口。

人工诊断可以在选定的常驻环境中直接调用：

```bash
python3 /opt/anchor-ci/tools/basic_tools/runner.py frontend_build \
  --context /workspace/tasks/TASK/context.json --parameters '{"jobs":2}' --execute
```

不带 `--execute` 只输出计划。诊断输出 `trusted_receipt=false`，不替代 broker 的
门禁凭据。构建并行度限制 1–64；失败后可降低并行度重试。默认清理当前 checkout
的 build/dist/egg-info，并清空该工具本次 wheel 输出；`build_mode=incremental`
保留 build。不会销毁容器、删除全局缓存或改写永久工具规则。

## 环境配置

`profile.tools` 支持：

* `python_bin` 是人工诊断的解释器后备配置；生产执行由主机 `context.python_bin`
  覆盖为受信任务入口，不直接启动可写 venv 的 Python。
* `llvm_dir`；已安装且与 profile 的 `llvm_revision` 对应的 LLVM 路径。
* `required_modules`，默认 `build/setuptools/wheel/pybind11`；`required_commands`，
  默认 `git/cmake/ninja`；`required_source_files`，默认空列表，由对应工具检查构建和 smoke 文件。
* `env`：受信环境变量；`env_scripts: [{path, args: []}]`：受信环境脚本。
  脚本可以激活 venv、设置厂商 SDK；每条命令都会加载，任务临时/缓存/转储目录会恢复。
* `backend_dir`、`backend_wheel_pattern`、`expected_backend`、`backend_smoke_argv`：
  已准备的后端 checkout、wheel 模式、Triton 发现名称和实际 JIT 测试 argv。
  argv 中 `{python}` 以及首项 `python`/`python3` 使用当前任务解释器；shell 包装脚本
  应使用 `PYTHON_BIN`，确保 JIT 与安装 wheel 使用同一个环境。
* `frontend_test_paths`：前端 pytest 测试根列表，默认 `["python/triton_anchor/tests"]`。
* `backend_test_paths`：后端 checkout 内的真实 pytest 测试根列表，3.0 使用后端套件时必须配置。
  不能提供可用后端时保留未配置并报告阻塞；示例路径不表示环境已经具备测试能力。
* `flaggems_dir`、`flaggems_pytest_args`（参数字符串，默认 `--ref cpu -vs`）、可选 `vendor`。

`frontend_tests`、`backend_tests` 接受 `paths`（1–100 个测试根以内的相对路径或
`tests/test_ops.py::test_add` 形式的 pytest node ID）与 `keyword`（最长 300 字符的
pytest `-k` 表达式）。不传 paths 时执行受信 profile 的完整测试根；不能通过参数
扩展到其它目录、替换命令或修改依赖。容器内再次检查真实路径，拒绝越界符号链接。
JUnit `tests.xml` 由实际 pytest 生成，受信辅助程序形成 `tests.json`，记录选择、
任务身份、文件哈希和用例计数。无用例、全跳过或存在失败/错误都不能通过。

候选 wheel 使用 `--no-deps` 安装。匹配的 Python/后端/LLVM 依赖由每日受信配方准备；
任务内环境修复应有执行证据，不能静默升级共享环境的永久配方。

FlagGems 默认 `mode=impact`，参数 `ops`、`categories` 是标识符数组。选测匹配
实际 pytest marker；未知标识符和无对应测试均失败。没有提供影响集合时，使用全部
历史可通过算子，取代随机 sample。显式受影响算子即使不在历史通过名单仍会执行。
`mode=full` 执行原全量算子清单，需要控制器从手动触发元数据提供 `manual_full=true`。
历史 TSV 是起始算子目录，不代表当前测试已通过；每次均实际执行。

性能参数 `kernels` 支持 `add/mm/softmax/layernorm`，`repeat` 范围 1–100，
`warmup` 范围 0–20。可信基线由控制器通过
`context.performance_baselines[tool_id]` 提供：
`{path, sha256, base_sha, profile_id, llvm_revision}`。工具验证基准提交、环境标识、
LLVM 与文件哈希再比较；控制器应仅选择 backend/FlagGems/容器配方一致的缓存。
没有基线不会从不同环境的历史数值推断回退，也不把“命令成功”写成“无性能回退”。

主机选择器 `runtime/performance.py` 首先检查维护者配置的
`profile.performance_baselines[tool_id]`，字段与上述 record 相同：`path` 是受信
容器内文件的绝对路径，`sha256` 是其完整内容哈希，另外三个身份字段必须匹配
当前任务的 base commit 及选定环境。主机不把容器路径当成本机文件打开；工具
执行时再次核验真实内容哈希。该配置不允许由 PR 或 agent 提供。

没有匹配显式基线时，选择器在 `state_dir/runs/` 自动查找已经成功发布的
运行。只有该运行的 tested SHA 等于当前 base SHA、repository/profile/LLVM
一致、对应工具存在成功 receipt，且冻结 artifact 清单中的
`artifacts/<tool>/candidate.json` 大小与 SHA-256 都正确，才复制到当前任务的
`baselines/<tool>.json`。较新的有效记录优先，损坏记录不会妨碍其他有效基线。
主机随后将基线目录设为 root 所有且只读。基线身份来自主机结果和账本，
不从 benchmark JSON 自称的 SHA 推断。只有候选测量、未发布结果或不匹配的
历史环境都不能成为基线；无匹配时保持 `baseline_available=false`。

`ai_review_tools/` 提供架构与专项审查要求；`ai_custom_tools/` 说明任务内复现、分析与证据处理的使用边界，脚本统一通过现有 broker 的 `custom_test` 调用。参数为相对 `artifacts/custom/` 的 Python `path`、字符串数组 `args`、1–900 秒的 `timeout`。分析成功不替代基础工具的必检结果。
这些工具的报告需要与 broker 命令事实关联，不能以提示词或文件存在代替实际执行。
