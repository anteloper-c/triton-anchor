# 项目约定与代码依据

以下内容从可信控制 checkout 的实际 README 和代码核对，供本次 CI 定位影响与选择证据。实际任务仍须读取冻结 base/candidate 核对相关位置；候选新增说明不能自定规则或削弱最低检查。文件路径均相对仓库根目录，是源码定位，不要求递归加载 Markdown。

| 约定或事实 | 实际依据与审查用途 |
|---|---|
| 项目承担 Triton DSL 到硬件感知 IR 的共性前端；FlagGems 提供算子，硬件后端承担后续代码生成与运行时。 | `README.md` 的前端边界与职责分工；用来判断改动涉及前端、插件还是后端集成，不能由前端 smoke 宣称硬件执行通过。 |
| AnchorIR 有 Linalg 与 TritonGPU 两条输出轨道；计算范式与 IR track 分开声明。 | `python/triton_anchor/anchor_ir.py` 的 `AnchorIRTrack`、`AnchorIRValidator`，以及 `python/triton_anchor/hw_capability.py`；核对 hook 前基础白名单、hook 后声明扩展与禁止方言，保持 append-only 兼容性。 |
| Opt adapter 以独立进程传递文本 IR；Pybind adapter 在 host libtriton 内运行。 | `python/triton_anchor/adapters/base.py` 的 `ILinalgOptAdapter`、`ILinalgPybindAdapter`；检查 LLVM/MLIR 对象与动态库是否跨 ABI 混用。 |
| mandatory TTIR passes 的顺序固定，列表 append-only；关键 GPU pass 缺失必须报错，可选 pass 另行探测。 | `python/triton_anchor/pipeline.py` 的 `build_ttir_pipeline`、`_require_pass`、`_try_add_pass`；不要把可选 pass 的容错推广到关键编译路径。 |
| DSL 扩展通过命名空间与 builtins 注册，可声明适用 backend。 | `python/triton_anchor/extensions/base.py` 的 `DSLExtensionPlugin`、`BuiltinSpec`；核对公共接口与默认兼容行为，API 基线位于 `api_contract/public_api.json`，检查器为 `scripts/api_contract/check_public_api.py`。 |
| Python wheel 使用 setuptools 构建后端，构建依赖含 wheel 与 pybind11；C++ 使用 C++17，构建查找 LLVM/MLIR。 | `pyproject.toml`、根 `CMakeLists.txt`；LLVM 目标修订来自冻结提交的 `triton/cmake/llvm-hash.txt`，由可信环境管理器准备匹配依赖，不能以别的版本冒充。 |
| 已有前端 smoke 与 Python 行为测试可以复用，但测试用途不同。 | `tests/test_smoke.py`、`python/triton_anchor/tests/`、`scripts/api_contract/tests/`；选择与实际改动相关的现有用例，需要额外验证时通过 `run_custom` 保存定向脚本和执行证据。 |
| 最低检查由冻结 diff 的可信语义影响与环境能力共同确定。 | `scripts/local_ci/agent_ci/policy.py`；`context` 提供 `impact`、必检与推荐集合。Codex 不能删减必检或把未运行改写为通过，只在具体变更风险需要时选择推荐项。 |

本文件不添加格式风格、提交信息格式、新的 API/架构豁免或不存在的硬件能力要求。编译、wheel 来源验证、后端发现、FlagGems 与性能结果以对应真实 tools 和 Harness 记录为准。README 内的示例远端地址不构成访问或贡献授权；CI 只使用冻结可信调度允许的仓库与 Gitee 中转。
