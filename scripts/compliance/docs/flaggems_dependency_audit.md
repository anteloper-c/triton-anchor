# FlagGems 固定子模块依赖核查

## 固定对象

- triton-anchor gitlink：`633d9111528d37e60d9804d2f4ac8d9e00c3af5c`；
- FlagGems commit tree：`623e945497203cbd81b4a71b1a242e46c7c6d493`；
- `setup.py` Git blob：`e87ebb877bab461eddf7de94dcaea5486503e26a`，SHA256 `bbdb954f661ce82da18f8e4fecb92629047f5f88b624258115b27055086f88c0`；
- `LICENSE` Git blob：`f18fcdbc8d7a85529a7ed882b1e21aef1602c921`，SHA256 `3d96ddb29c3f72e59a4d50d17455e8d2989d603587bbbc88ec069641551b302a`。

核查使用该提交的受控临时 checkout，只读取 Git tree、`setup.py` 和声明文件，没有执行子模块代码或安装它的依赖。

## 可识别直接依赖

| 分组 | 固定声明 |
|---|---|
| Python | `>=3.8.0` |
| 必需运行依赖 | 当前环境中的 `triton`、`triton-nightly` 或 `pytorch-triton` 之一，约束 `>=2.2.0`；`torch>=2.2.0`；未限定版本的 `PyYAML` |
| 构建依赖 | `setuptools` |
| `test` extra | `pytest>=7.1.0`、`numpy>=1.26`、`scipy>=1.14` |
| `example` extra | `transformers>=4.40.2` |

`setup.py` 自身把项目版本写为 `2.2`，项目 URL 写为 `https://github.com/FlagOpen/FlagGems`；triton-anchor 实际 gitlink 则来自 `https://github.com/RACE-org/FlagGems`。因此登记表保留实际 gitlink 仓库作为固定来源，同时把 FlagOpen URL 视为子模块项目元数据，不把两者静默合并成同一提交来源。

## 间接依赖边界

该提交没有 requirements lock、wheel lock、conda environment lock 或其他可固定解析结果的文件。源码只能证明上述直接约束，不能选择 PyYAML、SciPy、Transformers、PyTorch 或 Triton 的具体发行版本，也不能据此宣称它们的间接依赖已经闭包。

当前全量 audit 会生成根仓库 `dependency-inventory.json`，本次修正让 `audit` 真正消费这份证据；现有声明扫描器仍不会递归执行 FlagGems 的动态 `setup.py` 语义。登记表先保存本次静态核查得到的 test-only 事实，后续仍需在选定的兼容性测试环境中采集实际解析版本及可识别间接依赖。FlagGems 及其测试依赖不会因此进入核心 Wheel SBOM 或 Notice。
