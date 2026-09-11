# 既望环境接入说明

本目录保留 CI_dev 的实际配方模板、只读 FlagGems 配置和 slim 基础镜像构建脚本，统一入口见 ../README.md。这些文件是环境准备材料；真实 digest、LLVM revision、SDK 能力、Gitee 仓库和公司模型配置必须由目标主机实际验证。

使用固定控制发布经 Gitee 部署。候选环境通过前后端 build/install/smoke 后才供新任务使用；控制程序升级与昂贵依赖底座分别管理。每任务一个非 root 执行用户，模型会话与正式验证使用同一 candidate 工作目录。不得恢复旧四身份或常驻任务容器约束。

slim/configure.py 生成配置后仍须通过 ops_maint/preflight.py 及真实工具链验证，模板不是验收结果。所有运维 unit 和健康发布均由 ops_maint/install.py 生成，未执行安装或启动。
