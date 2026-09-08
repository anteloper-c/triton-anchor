# CI Dashboard

`local-ci.html` 展示新 Local CI 的任务、检查选择和未执行原因、AI 架构与专项审查、阻塞原因、性能变化、主机命令证据及 worker 健康。`index.html` 保留全量算子和后端性能历史视图，并链接到新页面。

从已获取的结果仓库 checkout 同步新页面数据：

```sh
python3 scripts/dashboard/sync_agent_results.py \
  --results-dir /path/to/results-checkout \
  --output-dir dashboard/data \
  --results-web-url https://gitee.com/OWNER/NEW-RESULTS-REPOSITORY \
  --results-branch local-ci-results
python3 -m http.server 8000 --directory dashboard --bind 127.0.0.1
```

打开 `http://127.0.0.1:8000/local-ci.html`。同步脚本只读取 checkout，不负责认证、fetch 或推送。GitHub Pages 工作流负责取得配置的结果分支。

- 结果来源：`runs/<task_id>/<run_id>/result.json`，Schema `triton-anchor-local-ci-result/v4`。
- 最新索引：`tasks/<task_id>/latest.json`，核验身份、相对路径与结果 SHA-256；不匹配时给出同步异常，不将其提升为最新通过结果。
- 健康来源：`health/<worker_id>.json`，Schema `triton-anchor-local-ci-worker-health/v2`。
- 页面数据：`data/local-ci.json`，Schema `triton-anchor-dashboard-local-ci/v1`。仓库初始文件是空 feed，未发布真实结果时明确显示暂无数据。

页面文本按文本节点呈现，不解释结果中的 HTML。日志链接仅接受 HTTPS。超过 15 分钟的 worker 心跳在页面标记为过期；邮件投递和整机离线告警由独立维护/监控服务完成，页面不是告警发送器。

`scripts/local_ci/tests/test_dashboard_agent.py` 包含发布索引、SHA-256、路径边界、健康异常、空结果和旧同步入口兼容性验证。其 `example_result()` 仅用于测试。任何界面预览 fixture 必须设置 `data_mode: "fixture"`，页面会显示醒目的样例声明；不得以 fixture 代替真实测试证据。
