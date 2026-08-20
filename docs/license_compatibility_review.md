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
| LLVM/MLIR | [llvm-project `159dcbfc...c7ca` LICENSE.TXT](https://github.com/llvm/llvm-project/blob/159dcbfcba88c9d3c7760b804f9b916b2a6bc7ca/LICENSE.TXT)，SHA256 `8d85c105...afee` | CMake 确认参与构建/链接；具体候选是静态嵌入还是动态外部依赖由构建清单和 ELF 决定 | Apache-2.0 WITH LLVM-exception | 官方项目声明已确认；实际构建版本、链接形态、应随包提供的文本和组合兼容性待审 |
| FlagGems | gitlink `633d9111528d37e60d9804d2f4ac8d9e00c3af5c`；[该提交 LICENSE](https://github.com/RACE-org/FlagGems/blob/633d9111528d37e60d9804d2f4ac8d9e00c3af5c/LICENSE)，SHA256 `3d96ddb2...302a` | 仅兼容性测试使用，没有复制或链接进核心 Wheel | Apache-2.0 | 上游声明已确认；当前核心 Wheel 不发生产品组合兼容性判断，若用途变化则重新审查 |

## 其他当前候选相关组件

| 组件 | 已确认事实 | 仍需关闭的问题 |
|---|---|---|
| f2reduce | vendored commit `949b91d022c001bbce19157f806013d37f05fbf5`，本地 `LICENCE.txt` 为 MIT，代码嵌入原生库 | ScanCode 对 README 给出含 GPL 的复合表达式；必须判断是文本误报、第三方代码提示还是额外义务，不能由 MIT 文件自动覆盖 |
| pybind11 | 构建依赖且头文件模板编入原生库 | 实际构建版本、固定来源、许可证文本与 concluded license |
| zlib | CMake 链接，当前实包 ELF 显示 `libz.so.1` 为外部运行依赖 | 实际构建/运行 ABI 来源、版本覆盖和许可证审查 |
| zstd | 当前实包 ELF 显示 `libzstd.so.1` 为外部运行依赖 | SONAME 不等于包版本或来源；需要人工覆盖记录，不能伪造 OSV 查询版本 |
| TTGPU 变体源码 | 头文件会被复制，开关启用时还会嵌入原生库 | 来源、所有权、版本和许可证均未解决，是当前明确阻断项 |

## 批准前必须形成的记录

- leader/许可证审查人批准完整 SPDX expression 和目标分发许可下的兼容性；
- 明确各分发/嵌入组件的许可证文本与版权归属如何随候选产物提供；
- 对源码扫描的复合或冲突表达式逐项给出可审计处置，不能维护静默忽略名单；
- 对未随产物分发的测试组件和动态运行库保留审计结论，但不把它们错误写进第三方分发声明；
- 策略批准后，将结论写回唯一 `component-registry.json`，再由它生成 `THIRD_PARTY_NOTICES.md` 和候选 SBOM。

本记录不复制许可证全文，也不批准任何风险接受；正式发布所需文本由经审查的组件登记表和 Notice 流程统一维护。
