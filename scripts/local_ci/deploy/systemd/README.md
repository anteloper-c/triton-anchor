# 用户级 systemd 示例

本目录 `.service` / `.timer` 文件由 `install.py` 根据 `config.example.json` 生成，仅供审阅；未安装或启动服务。包括常驻轮询的 worker、独立 health/retention 和三个可信镜像更新 timer。worker 没有 timer，不要再周期启动旧的 pull-and-run 脚本。

Harness 在宿主机普通 CI 用户下运行，按任务启动容器内的 Codex 和测试工具。无需单独部署 Codex 服务，也无需为容器内四个 UID 创建四个宿主账号。容器内新建/恢复 Codex 会话的 `danger-full-access` 模式由驱动设置，不在 systemd unit 中另加 Codex 启动命令。

`SET_ACTUAL_CI_USER`、`SET_ACTUAL_CI_UID` 都是必须替换的占位符。实际部署先填写私有配置，完成可信镜像和显式资源验证，再以普通 CI 用户运行 `install.py --config 实际配置 --credentials-env 实际凭据文件 --render-dir 审阅目录` 重新渲染；目标为该用户的 `~/.config/systemd/user`，不使用 sudo。安装和回滚只执行 `systemctl --user daemon-reload`，不启动或启用服务。

完整步骤见 [部署与回滚](../README.md)；仅能连接 Gitee 的 `jiwang_ci` 服务器见 [部署交接](../jiwang_ci/HANDOFF.md)。
