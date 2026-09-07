# 架构契约审查

规则来源必须取可信 base/控制版本；候选提交不能自行授予豁免。

| rule_id | 规则与现有实现依据 |
|---|---|
| abi-isolation | `python/triton_anchor/adapters/base.py`：Opt adapter 通过独立进程交换文本 IR；Pybind passes 属于同一 host libtriton，禁止混用不同 LLVM/MLIR ABI。 |
| anchor-ir-tracks | `python/triton_anchor/anchor_ir.py`：双轨隔离、hook 前后验证、扩展声明、allowed whitelist append-only；不能以性能 smoke 宽松容差代替跨 Adapter 数值契约。 |
| mandatory-pipeline | `python/triton_anchor/pipeline.py`：mandatory passes 保持顺序且 append-only，缺少必要 GPU pass 不可静默成功。 |
| plugin-compatibility | `python/triton_anchor/extensions/base.py`：独立插件注册和向后兼容扩展；公共 API 按可信 contract 检查。 |

架构违规证据包括 rule_id、具体变更位置、违反规则的直接代码或执行证据。普通风格意见、未定义的新架构偏好不能阻断。当前环境无法验证的多后端性质应明确标注，不能由单一后端 smoke 推断。
