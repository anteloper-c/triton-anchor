#!/usr/bin/env bash
set -euo pipefail

if (($# > 1)); then
  echo "用法：setup_codex_ai_container.sh [local-ci-container]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_container="${1:-${LOCAL_CI_CONTAINER:-anchor-sophgo-ci}}"
codex_ai_ci_home="${CODEX_AI_CI_HOME:-}"
host_codex_bin="${CODEX_BIN:-}"
python_bin="${PYTHON_BIN:-python3}"
credentials_validator="${SCRIPT_DIR}/validate_codex_ai_credentials.py"
if [[ -z "${host_codex_bin}" ]]; then
  host_codex_bin="$(command -v codex 2>/dev/null || true)"
elif [[ "${host_codex_bin}" != */* ]]; then
  host_codex_bin="$(command -v "${host_codex_bin}" 2>/dev/null || true)"
fi

case "${source_container}" in
  "" | *[!A-Za-z0-9_.-]*)
    echo "Local CI 容器名称无效：${source_container}" >&2
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker 命令。" >&2
  exit 1
fi
if [[ -z "${host_codex_bin}" || ! -x "${host_codex_bin}" ]]; then
  echo "宿主机上找不到可执行的 Codex CLI，请设置 CODEX_BIN。" >&2
  exit 1
fi
if [[ -z "${codex_ai_ci_home}" ]]; then
  echo "必须设置独立的 CODEX_AI_CI_HOME。" >&2
  exit 1
fi
if ! command -v "${python_bin}" >/dev/null 2>&1; then
  echo "宿主机找不到 Python：${python_bin}" >&2
  exit 1
fi
if [[ ! -r "${credentials_validator}" ]]; then
  echo "独立凭据校验器不可读：${credentials_validator}" >&2
  exit 1
fi
"${python_bin}" "${credentials_validator}" \
  --codex-home "${codex_ai_ci_home}" \
  --personal-codex-home "${HOME}/.codex" \
  --quiet
if [[ "$(docker inspect --format '{{.State.Running}}' "${source_container}" 2>/dev/null || true)" != "true" ]]; then
  echo "Local CI 容器未运行：${source_container}" >&2
  exit 1
fi
if docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
  "${source_container}" | grep -Fxq '/var/run/docker.sock'; then
  echo "Local CI 容器挂载了 Docker socket，不能作为 Codex 临时容器来源。" >&2
  exit 1
fi
if ! docker exec --user 0 "${source_container}" test -d /workspace; then
  echo "Local CI 容器中不存在 /workspace：${source_container}" >&2
  exit 1
fi

cat <<EOF
Codex AI CI 前置检查通过。

- Local CI 容器：${source_container}
- 宿主机 Codex CLI：${host_codex_bin}
- Codex AI CI 独立凭据目录：${codex_ai_ci_home}
- Local CI 工作区：/workspace

每次任务将由 run_codex_ai_ci.sh 临时执行 commit、run、copy、exec、collect 和 cleanup；本脚本不会创建长期容器、镜像或 volume。
EOF
