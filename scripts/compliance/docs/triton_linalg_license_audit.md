# triton-linalg vendored tree 与许可证核对

## 核对边界

本记录只回答两个可由仓库证据验证的问题：当前 `triton-anchor` 中的 triton-linalg vendored surface 与哪个官方提交对应、两者有哪些差异；以及该官方提交提供了什么许可证和归属声明。它不代替许可证兼容性审批，也不把“上游声明 Apache-2.0”自动等同于“当前派生树已满足全部再分发义务”。

对比双方均固定到不可变提交：

- 本地基线：RACE-org `main@9e00777b3df51cab3dd4addcc37cc6c959f6a712`；
- 官方上游：[Cambricon/triton-linalg `master@00f51c2e48a943922f86f03d58e29f514def646d`](https://github.com/Cambricon/triton-linalg/tree/00f51c2e48a943922f86f03d58e29f514def646d)；
- 上游提交作者时间：`2025-02-06T03:21:58Z`，提交者时间：`2025-02-07T17:08:27+08:00`；
- 上游提交 tree：`8481a301538e9235b26493c1ae6893912e5be4fc`。
- 当前本地 include tree：`cb73c58bcdccde8b8e9baebe7d500c6ad314699a`；当前本地 lib tree：`ab55e8b45bb83f6f1fcfaeca97a3cb6a58c1d330`。

对比使用 Git tree 的文件模式和 blob 标识，不依赖工作区换行转换。路径映射固定为：

| 官方上游 | 本地 vendored surface |
|---|---|
| `include/triton-linalg/**` | `csrc/include/triton-linalg/**` |
| `lib/**` | `csrc/lib/triton-linalg/**` |
| `bin/RegisterTritonLinalgDialects.h` | `csrc/include/triton-linalg/RegisterTritonLinalgDialects.h` |

## 完整差异摘要

上游提交共有 160 个 tracked entries。按上述映射，118 个文件属于本地 vendored surface，且 118 个均能在本地找到：

- 114 个映射文件的 blob 内容相同；
- 4 个映射文件的 blob 内容不同；
- 没有映射文件缺失；
- 117 个 `include/triton-linalg/**` 或 `lib/**` 文件从上游模式 `100644` 变为本地 `100755`；重定位的注册头文件仍为 `100644`；
- 其余 42 个上游 entries 未被 vendored，完整列表见下文。

### 内容不同的 4 个文件

| 上游路径 → 本地路径 | 行变化 | 可观察差异 |
|---|---:|---|
| `lib/Conversion/CMakeLists.txt` → `csrc/lib/triton-linalg/Conversion/CMakeLists.txt` | `+1/-1` | 文本相同，仅本地文件末尾缺少换行 |
| `lib/Conversion/TritonToLinalg/TritonToLinalg.cpp` → `csrc/lib/triton-linalg/Conversion/TritonToLinalg/TritonToLinalg.cpp` | `+23/-2` | 为 `MaxNumFOp`、`MinNumFOp` 选择正负无穷 identity，其他操作仍调用 `getNeutralElement`；另加入未使用的 `module_op` 局部变量 |
| `lib/Dialect/LinalgExt/Utils/CMakeLists.txt` → `csrc/lib/triton-linalg/Dialect/LinalgExt/Utils/CMakeLists.txt` | `+0/-2` | 删除 `DEPENDS` 与 `TritonTableGen` |
| `lib/Pipelines/Pipelines.cpp` → `csrc/lib/triton-linalg/Pipelines/Pipelines.cpp` | `+13/-0` | 增加 include/using/格式差异，并在 pipeline 末尾追加 CSE 与 canonicalizer，注册函数增加显式 `return` |

上述“行变化”来自两个 Git blob 的 `git diff --numstat`，因此末尾换行也会记作一增一删。4 个文件同时包含在前述 `100644` → `100755` 的模式变化中。

### 内容相同但发生重定位的文件

`bin/RegisterTritonLinalgDialects.h` 被放到 `csrc/include/triton-linalg/RegisterTritonLinalgDialects.h`，两者 blob 均为 `7bfe49af9d5e80be9a0aa860d9f65183197a5615`，模式均为 `100644`。

### 未 vendored 的 42 个上游 entries

```text
.github/ci_script/file_guard.py
.github/ci_script/triton-linalg-ci_script.sh
.github/workflows/triton-linalg_ci.yaml
.gitignore
.gitmodules
ACKNOWLEDGMENTS
CMakeLists.txt
CODE_OF_CONDUCT.md
LICENSE
README.md
backend/compiler.py
backend/driver.py
backend/name.conf
bin/CMakeLists.txt
bin/triton-linalg-opt.cpp
include/CMakeLists.txt
test/CMakeLists.txt
test/Conversion/arith-to-linalg.mlir
test/Conversion/load.mlir
test/Conversion/math-to-linalg.mlir
test/Conversion/triton-to-linalg.mlir
test/Dialect/Auxiliary/invalid.mlir
test/Dialect/Auxiliary/ops.mlir
test/Dialect/LinalgExt/canonicalize.mlir
test/Dialect/LinalgExt/invalid.mlir
test/Dialect/LinalgExt/ops.mlir
test/Dialect/Triton/canonicalize-load.mlir
test/Dialect/Triton/canonicalize-mask-ops.mlir
test/Dialect/Triton/canonicalize-tt-broadcast-to-broadcast.mlir
test/Dialect/Triton/extract-move-backward.mlir
test/Dialect/Triton/extractslice-move-backward.mlir
test/Dialect/Triton/ptr-strength-reduction.mlir
test/Dialect/Triton/wrap-func-body-with-single-block.mlir
test/Pipelines/pipeline.mlir
test/lit.cfg.py
test/lit.site.cfg.py.in
tools/ci/daily/triton-linalg_daliy.pipeline
tools/scripts/lint_check/common.sh
tools/scripts/lint_check/format_diff.py
tools/scripts/lint_check/lint.sh
tools/scripts/test_triton-linalg.sh
triton
```

其中 `triton` 在上游是指向 `757b6a61e7df814ba806f498f8bb3160f84b120c` 的 gitlink；它不属于本记录映射的 triton-linalg vendored surface。`LICENSE` 与 `ACKNOWLEDGMENTS` 未随该 subtree 一同 vendored，是下述审查缺口的一部分。

## 官方许可证与归属证据

不在本文件重复许可证全文，只固定不可变证据：

| 证据 | Git blob | 可确认事实 |
|---|---|---|
| [上游 `LICENSE`](https://github.com/Cambricon/triton-linalg/blob/00f51c2e48a943922f86f03d58e29f514def646d/LICENSE) | `fe41bf739fd41c7e71c7e7b7ea7d7cda4256c5e2` | 文件给出 Cambricon 2022–2025 版权声明，并载有 Apache License 2.0 |
| [上游 `ACKNOWLEDGMENTS`](https://github.com/Cambricon/triton-linalg/blob/00f51c2e48a943922f86f03d58e29f514def646d/ACKNOWLEDGMENTS) | `8f2098ad6bf57ff573c09c94c46a18ffeb121278` | 声明项目采用 Apache License 2.0，但第三方组件除外；列出 Triton 与 triton-shared 的 MIT 归属 |
| 本地映射源码版权头 | 见 `csrc/include/triton-linalg/**`、`csrc/lib/triton-linalg/**` | 多个文件保留 Cambricon 版权标记；这不能替代完整许可证和第三方归属审查 |

这些证据足以把 `Apache-2.0` 记录为上游的 declared license，并把官方来源固定到上述仓库与提交；不足以填写本地派生树的 concluded license，也不足以批准与整个 `triton-anchor` 发布组合的兼容性。

## 尚未关闭的审查项

两个本地 tree id 可以稳定标识本次核查的派生内容，但不是 Cambricon 上游 release 或独立语义版本。在以下事项完成前，`component-registry.json` 中 triton-linalg 的版本仍为 `unresolved`，许可证状态仍为 review pending：

1. 确认 4 个内容修改文件所需的修改声明和归属方式，并审查 117 个可执行位变化是否应保留；
2. 确认上游 `LICENSE` 与 `ACKNOWLEDGMENTS` 应如何随 Wheel/源码发布物提供，且不会被项目根许可证或自动生成 Notice 错误替代；
3. 将 `ACKNOWLEDGMENTS` 中 Triton、triton-shared 的归属与实际 vendored 文件边界逐项核对；不能只凭项目级声明推导每个文件的 concluded license；
4. 由 leader/许可证审查人批准最终完整 SPDX expression、Notice 内容和组合兼容性。

本核对没有改变源码、文件模式或上游引用，只补充可审计证据。任何后续上游升级或本地 vendored 文件变化都必须重新执行 tree 对账，而不能沿用本记录的计数。
