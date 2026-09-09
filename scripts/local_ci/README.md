# Local CI

Codex 在每个 Triton 版本的固定常驻容器内理解 PR、选择工具、补充用例并完成架构审查。主机 Poller 校验任务，证据 broker 执行并记录命令；Gitee 保存任务与结果，GitHub 校验后更新必要检查和 Dashboard。

产品定位、验证能力、反馈与边界见 [CI 产品说明](../../docs/ci_guide_zh.md)。本目录保留以下入口：

| 入口 | 内容 |
| --- | --- |
| [ai_ci_program.md](ai_ci_program.md) | Codex 的任务编排与审查要求 |
| [config.example.json](config.example.json) | 主机受信配置示例，部署前替换实际值 |
| [tools/README.md](tools/README.md) | 13 个基础工具、参数、依赖和适用能力 |
| [control/deploy/README.md](control/deploy/README.md) | 常驻镜像、systemd 服务、健康发布及独立监控 |

```text
scripts/local_ci/
├── ci.py                    # Poller 入口
├── ai_ci_program.md         # Codex 编排
├── config.example.json      # 主机配置
├── deepseek-models.json     # 临时 DeepSeek 模型目录
├── control/                 # 控制面实现
│   ├── runtime/             # 任务执行、证据与恢复
│   ├── integration/         # 投递、结果校验及回写
│   ├── schemas/             # 结果和审查结构
│   └── deploy/              # 配方和服务模板
├── tools/
│   ├── basic_tools/         # 真实可调用的基础检查
│   ├── ai_review_tools/     # 架构与专项审查
│   └── ai_custom_tools/     # 任务内复现、分析与证据处理
├── maintenance/             # 容器维护、健康与外部监控
└── tests/                   # 控制面测试
```

Local CI 控制面位于本目录和 `scripts/dashboard/`。`scripts/ci/`、`scripts/api_contract/` 保留 GitHub 基础检查与 API 检查能力。被测 PR 的同名文件不能替换正在运行的受信控制程序。

从仓库根目录运行：

```sh
python3 scripts/local_ci/ci.py poll --config /etc/anchor-ci/config.json --once
python3 -m pytest -q scripts/local_ci/tests scripts/ci/tests
```

只有 Triton 3.0 支持后端、算子和性能检查；该环境的真实 SDK、后端及测试目录仍需配置。缺少配置明确失败，不能以占位命令或合成测试替代真实通过。认证、原始设计、用户提示词与操作记录保存在仓库之外。
