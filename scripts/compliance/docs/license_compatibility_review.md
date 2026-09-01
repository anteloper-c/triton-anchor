# T8.2 许可证与兼容性核对记录

## 结论边界

本记录落实 T8.2 对 Triton、triton-shared、triton-linalg、LLVM/MLIR、FlagGems 等组件的许可证核对。它把三件事分开：

1. 固定提交的官方许可证文件是可验证的上游事实；
2. 当前候选是否实际分发、嵌入、运行时依赖或仅用于测试，由源码、Wheel、构建和 ELF 证据决定；
3. `concluded license`、组合兼容性和风险接受属于人工审批结论，扫描器不能代替。

项目当前许可证策略仍是 `pending`。因此下表中的上游声明即使已确认，也不代表候选已获准晋级。

## 原任务点名组件

| 组件 | 固定证据 | 当前用途结论 | 上游声明 | 本项目结论 |
|---|---|---|---|---|
| Triton | vendored marker `757b6a61e7df814ba806f498f8bb3160f84b120c`；[该提交 LICENSE](https://github.com/triton-lang/triton/blob/757b6a61e7df814ba806f498f8bb3160f84b120c/LICENSE)，SHA256 `92640fb9...fb946` | Python/头文件随 Wheel 分发，部分 C++ 代码嵌入 `libtriton.so` | MIT | 上游声明已确认；本地没有随 vendored tree 保存该 LICENSE，且源码扫描存在保留其他许可证的文件，最终 expression、归属和兼容性待审 |
| triton-shared | [microsoft/triton-shared `08684f92...a3e4` LICENSE](https://github.com/microsoft/triton-shared/blob/08684f92ad30696362dce1760a83be889639a3e4/LICENSE)，SHA256 `c2cfccb8...5383` | 当前只在文档/上游归属中出现，没有进入核心 Wheel、构建或运行依赖图 | MIT | 已完成“当前不属于候选产物”的核对；若以后成为实际依赖，必须固定实际 revision 并重新做产品兼容性审查 |
| triton-linalg | 官方基线 `00f51c2e48a943922f86f03d58e29f514def646d` 的 [LICENSE](https://github.com/Cambricon/triton-linalg/blob/00f51c2e48a943922f86f03d58e29f514def646d/LICENSE) 与 [ACKNOWLEDGMENTS](https://github.com/Cambricon/triton-linalg/blob/00f51c2e48a943922f86f03d58e29f514def646d/ACKNOWLEDGMENTS) | 头文件随 Wheel 分发，代码嵌入原生库；本地是含修改的派生树 | Apache-2.0，且 ACKNOWLEDGMENTS 单列 MIT 第三方归属 | 官方声明已确认；4 个内容修改、117 个文件模式变化、缺失上游 LICENSE/ACKNOWLEDGMENTS 的处置及最终兼容性待审，详见 [triton_linalg_license_audit.md](triton_linalg_license_audit.md) |
| LLVM/MLIR | 仓库 pin `10dc3a8e916d73291269e5e2b82dd22681489aa1`；[该提交 LICENSE.TXT](https://github.com/llvm/llvm-project/blob/10dc3a8e916d73291269e5e2b82dd22681489aa1/LICENSE.TXT)，SHA256 `8d85c105...afee` | CMake 确认参与构建/链接；同次构建证据记录该 source commit，并将 `llvm-config` 输出单独保留为工具版本；静态嵌入或动态外部依赖仍由 ELF 决定 | Apache-2.0 WITH LLVM-exception | 官方项目声明及当前源码 revision 已确认；应随包提供的文本和组合兼容性待审 |
| FlagGems | gitlink `633d9111528d37e60d9804d2f4ac8d9e00c3af5c`；[该提交 LICENSE](https://github.com/RACE-org/FlagGems/blob/633d9111528d37e60d9804d2f4ac8d9e00c3af5c/LICENSE)，SHA256 `3d96ddb2...302a`；固定依赖见 [flaggems_dependency_audit.md](flaggems_dependency_audit.md) | 仅兼容性测试使用，没有复制或链接进核心 Wheel；直接依赖和 extras 保留在 test-only 审计面 | Apache-2.0 | 上游声明已确认；当前核心 Wheel 不发生产品组合兼容性判断，测试环境的具体版本和间接依赖仍待选定，若产品用途变化则重新审查 |

## 其他当前候选相关组件

| 组件 | 已确认事实 | 仍需关闭的问题 |
|---|---|---|
| f2reduce | vendored commit `949b91d022c001bbce19157f806013d37f05fbf5`，本地 `LICENCE.txt` 为 MIT，代码嵌入原生库 | ScanCode 的 GPL 命中只来自 README 中“MIT-licenced rather than GPL-licenced”的比较文字，源码文件没有对应 GPL 命中；该路径级事实已核实，但正式处置仍须审查记录，不能添加静默忽略或由工具自动批准 |
| pybind11 | 构建依赖且头文件模板编入原生库；2026-08-24 Hosted audit 观察到 3.1.0，官方 PyPI 元数据和该 tag LICENSE 均声明 BSD-3-Clause | `pyproject.toml` 没有固定全局版本，未来候选仍取同次构建证据；concluded license 和随包文本待审 |
| build | 手工 audit `33377618999` 观察到 1.6.0；该版本官方 PyPI `License-Expression` 和 tag LICENSE 均为 MIT | 仅确认本次构建工具的上游声明；版本继续由同次构建证据确定，concluded license 待审 |
| uv | 当前 Hosted Runner 工作流固定选择 0.12.8；官方 PyPI `License-Expression` 和固定 tag 的 `LICENSE-MIT`、`LICENSE-APACHE` 均支持 `MIT OR Apache-2.0`；普通运行 `33464818866` 和手工 audit `33465696798` 已验证构建、OSV 和 audit 数据流 | 双许可证选择、兼容性和 concluded license 待人工审查 |
| setuptools | 同一 audit 观察到 84.0.0；该版本官方 PyPI `License-Expression` 和 tag LICENSE 均为 MIT；仓库还确认源码中的可选运行时导入 | 包级声明不代替其 bundled 第三方归属审查，也不说明运行环境一定安装；concluded license 待审 |
| wheel | 同一 audit 观察到 0.48.0；该版本官方 PyPI `License-Expression` 和 tag `LICENSE.txt` 均为 MIT | 当前是构建期组件，不进入分发 Notice；未来候选版本和 concluded license 待审 |
| packaging | 同一 audit 观察到 26.3；官方 PyPI 声明 `Apache-2.0 OR BSD-2-Clause`，tag 同时提供总说明、Apache-2.0 和 BSD-2-Clause 文本 | 双许可证选择、项目策略 expression 和 concluded license 待人工审查；当前是构建期间接依赖，不进入分发 Notice |
| pyproject-hooks | 同一 audit 观察到 1.2.0；PyPI 只有 MIT classifier、没有 SPDX `License-Expression`，tag LICENSE 为 MIT | 登记的 declared MIT 同时依赖 classifier 和固定 tag 文本；未来候选版本及 concluded license 待审 |
| zlib | CMake 链接，当前 audit Wheel 的 ELF 显示 `libz.so.1` 为外部运行依赖；官方 v1.3.1 LICENSE 固定为 Zlib 参考证据 | 官方参考版本不能代替候选使用的 Ubuntu 源包、版本覆盖和许可证结论 |
| zstd | 较早候选出现 `libzstd.so.1`，2026-08-24 audit Wheel 明确 absent | 每份候选必须按自身 ELF 分类；SONAME 不等于包版本或来源，需要人工覆盖记录，不能伪造 OSV 查询版本 |
| TTGPU 变体源码 | 17 个文件均能映射到固定 Triton `757b6a6` 文件且包含本地修改，详见 [ttgpu_provenance_audit.md](ttgpu_provenance_audit.md)；头文件会被复制，开关启用时还会嵌入原生库 | 已确认本地派生关系，但修改权属、独立版本、许可证结论和 Notice 仍未解决，是当前明确阻断项 |

## 批准前必须形成的记录

- leader/许可证审查人批准完整 SPDX expression 和目标分发许可下的兼容性；
- 明确各分发/嵌入组件的许可证文本与版权归属如何随候选产物提供；
- 对源码扫描的复合或冲突表达式逐项给出可审计处置，不能维护静默忽略名单；
- 对未随产物分发的测试组件和动态运行库保留审计结论，但不把它们错误写进第三方分发声明；
- 策略批准后，将结论写回唯一 `component-registry.json`，再由它生成 `THIRD_PARTY_NOTICES.md` 和候选 SBOM。

本记录不复制许可证全文，也不批准任何风险接受；正式发布所需文本由经审查的组件登记表和 Notice 流程统一维护。
