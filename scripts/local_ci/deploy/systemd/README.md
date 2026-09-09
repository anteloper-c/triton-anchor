# 用户级 systemd 示例

本目录 `.service` / `.timer` 文件由 `install.py` 根据 `config.example.json` 生成，仅供审阅；未安装或启动服务。包括 worker、独立 health/retention 和三个受信镜像更新 timer。

SET_ACTUAL_CI_USER/UID 都是必须替换的占位符。实际部署先填写私有配置，完成受信镜像和显式资源验证，再以普通 CI 用户运行 `install.py --config 实际配置 --credentials-env 实际凭据文件 --render-dir 审阅目录` 重新渲染；目标为该用户的 `~/.config/systemd/user`，不使用 sudo。安装和回滚只执行 `systemctl --user daemon-reload`，不启动或启用服务，详见上级 README。
