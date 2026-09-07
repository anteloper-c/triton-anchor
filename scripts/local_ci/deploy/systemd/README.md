# systemd 示例

本目录九个 `.service` / `.timer` 文件由 `install.py` 的默认渲染函数根据 `config.example.json` 生成，仅供审阅；未安装或启动服务。

示例使用 `/etc/triton-anchor-local-ci/config.json`、`/etc/triton-anchor-local-ci/credentials.env` 和 `/opt/local-ci/control/triton-anchor`。实际部署须先填写受信服务器配置并运行部署预检，再用 `install.py --config 实际配置 --credentials-env 实际凭据文件 --render-dir 审阅目录` 重新渲染。安装和回滚步骤见上级 `README.md`。
