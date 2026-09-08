# Local CI v4 本机模拟验收

结果：通过；通过测试 532 项。

| 测试集 | 结果 | 通过数 | 日志 |
| --- | --- | ---: | --- |
| worker-mcp-recovery | pass | 95 | worker-mcp-recovery.log |
| real-tool-interfaces | pass | 26 | real-tool-interfaces.log |
| persistent-environments | pass | 44 | persistent-environments.log |
| deployment-monitoring | pass | 51 | deployment-monitoring.log |
| github-gateway | pass | 27 | github-gateway.log |
| retained-local-contracts | pass | 235 | retained-local-contracts.log |
| api-contracts | pass | 9 | api-contracts.log |
| frontend-performance-dashboard | pass | 45 | frontend-performance-dashboard.log |

验证真实控制逻辑与工具接口，外部边界使用 fixtures。未验证真实模型、LLVM/后端构建、硬件、实际邮件或线上 GitHub/Gitee 回写。

Source digest: c6a568f856749dce889976aaf2ccba45e6c3cb9366ba3a2734f033178d83744d


验收后仅补充 deploy/README.md 的回滚说明，执行代码未改。

Delivery source digest: 7d2fbf52df9e73cb561b32eded1de0393eff62f796ea0023941e3607aaa3660e
