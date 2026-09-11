# CI Dashboard

提供三个业务视图：`local-ci.html` 的任务与证据，`index.html` 的全量算子和后端与性能。
前端统一读取 Gateway 短作业生成的 `data/tasks.json`（`triton-anchor-dashboard`）；
`data.js` 将统一结果投影到三个界面，保留选测/未执行原因、审查依据、失败详情、筛选、分页及 CSV/XLSX 下载。

结果与所选文件随同一 Git 提交发布。页面展示检查说明、审查结论和文件链接；未选中或超预算的文件保留在主机，并说明省略原因。
全量算子视图只显示真实 full FlagGems 结果；性能读取任务测量和同条件比较，无数据时明确留空。
仓库中的初始 feed 为空，没有展示样例成功数据。

```bash
python3 -m http.server 8000 --directory dashboard --bind 127.0.0.1
```

Gateway 发布时将 `_site/data/tasks.json` 复制到页面的 `data/`。
Worker/健康界面已移到独立 `scripts/local_ci/maintenance/health_site/`，业务页面没有 Worker 第四模块、运维 Issue 或邮件入口。
本地静态预览不代表已部署 Pages，也不代表真实工具链或生产门禁验收通过。
