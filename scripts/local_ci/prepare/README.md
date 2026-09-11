# 环境准备与部署

`prepare/` 管理 Rootless Docker 环境、每任务容器和服务安装。部署使用固定控制提交、镜像 digest、完整 LLVM revision 和服务器预置依赖。源码及控制更新经 Gitee 到达 CI 主机。

填写 `config.example.json` 或 `profiles/config.template.json` 中的实际路径、Gitee 仓库、镜像、依赖和模型配置。`branch_profiles` 将目标分支映射到 `profiles`；目标分支与 profile 同名时可直接选择。所有 PR 目标分支都可使用，但每个目标需要对应的环境配置。

以普通 CI 用户执行一次安装：

```bash
python3 scripts/local_ci/prepare/install.py \
  --config /absolute/path/config.json \
  --credentials-env /absolute/path/credentials.env --apply
```

安装器读取私有 `KEY=value` 凭据文件，值含空格时使用引号；文件必须属于 CI 用户且权限为 600。安装器检查配置，按配方复用或准备各 profile 的镜像，实测 Rootless Docker 资源限制，然后安装并启动 Worker、health、watchdog、retention 用户服务和定时器。已有 units 会备份，可用 `--rollback <备份目录> --apply` 恢复。机器需要已有的 Rootless Docker 用户服务和持久用户会话。

不加 `--apply` 输出安装计划；`--render-dir <目录>` 保存 units。`preflight.py --config <配置> --configuration-only` 可单独检查配置；`--probe-runtime` 实测已准备环境。依赖或配方更新时可运行 `rotate.py --config <配置> --profile <名称>`；环境准备按需执行，日常不定时重建镜像。以上入口均支持 `--help`。

每任务一个容器，Codex、构建和测试共用 `identities.task` / `identities.gid` 的非 root 身份。candidate、base 和临时实验是任务内的数据目录。可写挂载只有 `work/<task>/<run> → /task` 与 `runs/<task>/<run>/artifacts → /task/artifacts`；控制快照和 LLVM、FlagGems、后端等服务器依赖只读挂载。状态、私有日志和已封存结果留在宿主。

`runtime.py` 负责镜像及容器生命周期；`container_fs.py` 在容器内准备工作目录、独立 venv 和私有 Codex 会话，结束后清理凭据。任务命令可写自己的源码和构建输出；取消、超时和重启恢复由 Worker 停止对应任务，清理不删除已经保存的结果。

只读依赖的目录、权限和摘要配置见 [DEPENDENCY_MOUNTS.md](DEPENDENCY_MOUNTS.md)。`profiles/slim/` 提供共享基础镜像配方，FlagGems 从固定服务器目录导入，各任务的 venv 和构建输出独立。后端测试默认路径为 `tests`；多个路径可显式设置 profile 的 `tools.backend_test_paths` 数组。

健康采集、异常观察和本地保留策略见 [maintenance/README.md](../maintenance/README.md)。
