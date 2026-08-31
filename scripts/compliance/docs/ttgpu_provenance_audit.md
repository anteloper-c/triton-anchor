# TTGPU 变体源码来源核查

## 核查边界

本记录只固定 `csrc/include/ttgpu/**` 与 `csrc/lib/ttgpu/**` 可由当前 Git 历史证明的来源关系，不批准许可证、修改权属或组合兼容性。

## 可复核事实

- 17 个 TTGPU 文件均由 triton-anchor 提交 `269e7bd9e2e51cd67b3c19e151b9928588f5a809` 首次加入，提交说明为 `add special ttgpu support`；
- 同一提交中的 `triton/TRITON_VERSION` 固定 Triton `757b6a61e7df814ba806f498f8bb3160f84b120c`；
- 4 个 `csrc/include/ttgpu/**` 文件可逐一映射到该 Triton revision 的 `triton/include/**`；
- 12 个 `csrc/lib/ttgpu/**` 文件可逐一映射到该 revision 的 `triton/lib/**`，`csrc/lib/ttgpu/ir.cc` 可映射到 `triton/python/src/ir.cc`；
- 17 个映射文件都包含本地内容修改；以 Git blob 比较，首次引入时合计相对基线约为 `+1060/-277`；
- 当前本地内容由两个 Git tree 固定：`csrc/include/ttgpu` 为 `2c2182112b6bde7916b3871a40b53875d94b0440`，`csrc/lib/ttgpu` 为 `2aae5f337de8f34955b516d811abea4cb718cb70`。

上述证据足以把 TTGPU 记录为“基于固定 Triton revision 的本地派生源码面”，不能把它记录为某个未找到的独立上游 release 或原样 Triton checkout。

## 尚未关闭

1. Git 历史没有给出这组修改的外部上游 URL、独立版本或完整权属说明；
2. Triton 基线的 MIT 声明不能由扫描器自动升级为本地派生面的 `concluded license`；
3. 需要许可证审查人确认修改归属、完整 SPDX expression、随包许可证文本和 Notice 内容；
4. 在这些结论批准前，`component-registry.json` 中的来源、版本和许可证仍保持未解决并继续阻断候选。

本核查没有改动 TTGPU 源文件，也没有把 Git tree id 当成上游版本或发布批准。
