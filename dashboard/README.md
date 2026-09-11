# CI Dashboard

保留 ci_repo 的三个业务视图：`local-ci.html` 的任务与证据，`index.html` 的全量算子和后端与性能。
前端统一读取 Gateway 短作业生成的 `data/v4-tasks.json`（`triton-anchor-dashboard/v4`）；
`v4-data.js` 将统一结果投影到三个界面，保留选测/未执行原因、审查依据、失败详情、筛选、分页及 CSV/XLSX 下载。

验证结果与证据交付分开展示。必要交付未 ready 时不显示整体通过；附件 pending/expired 没有可用下载链接。
全量算子视图只显示真实 full FlagGems 结果；性能读取已验证测量和同条件比较，无数据时明确留空。
仓库中的初始 feed 为空，没有展示样例成功数据。

```bash
python3 -m http.server 8000 --directory dashboard --bind 127.0.0.1
```

Gateway 发布时将 `_site/data/v4-tasks.json` 复制到页面的 `data/`。
Worker/健康界面已移到独立 `ops_maint/health_site/`，业务页面没有 Worker 第四模块、运维 Issue 或邮件入口。
本地静态预览不代表已部署 Pages，也不代表真实工具链或生产门禁验收通过。
