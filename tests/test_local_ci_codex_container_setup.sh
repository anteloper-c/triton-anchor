#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
setup_script="${repo_root}/scripts/local_ci/setup_codex_ai_container.sh"
test_root="$(mktemp -d /tmp/local-ci-codex-container-setup.XXXXXX)"
trap 'rm -rf -- "${test_root}"' EXIT
mkdir -p "${test_root}/bin" "${test_root}/home/.codex"
printf 'model = "test"\n' > "${test_root}/home/.codex/config.toml"
printf '{}\n' > "${test_root}/home/.codex/auth.json"
printf '#!/usr/bin/env bash\necho codex-test\n' > "${test_root}/codex"
chmod +x "${test_root}/codex"

cat > "${test_root}/bin/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "${FAKE_DOCKER_LOG}"
printf '\n' >> "${FAKE_DOCKER_LOG}"
case "$1" in
  inspect)
    if [[ "$*" == *State.Running* ]]; then
      printf 'true\n'
    elif [[ "$*" == *Destination* ]]; then
      printf '/workspace\n'
    fi
    ;;
  exec)
    [[ "$*" == *"test -d /workspace"* ]]
    ;;
  *)
    echo "前置检查不应执行资源创建命令：$*" >&2
    exit 2
    ;;
esac
SH
chmod +x "${test_root}/bin/docker"

PATH="${test_root}/bin:${PATH}" \
HOME="${test_root}/home" \
CODEX_BIN="${test_root}/codex" \
FAKE_DOCKER_LOG="${test_root}/docker.log" \
  "${setup_script}" > "${test_root}/setup-output.txt"

grep -Fq "Codex AI CI 前置检查通过" "${test_root}/setup-output.txt"
grep -Fq "Local CI 容器：anchor-sophgo-ci" "${test_root}/setup-output.txt"
grep -Fq "本脚本不会创建长期容器、镜像或 volume" "${test_root}/setup-output.txt"
grep -Fq "inspect" "${test_root}/docker.log"
grep -Fq "exec --user 0 anchor-sophgo-ci test -d /workspace" \
  "${test_root}/docker.log"
if grep -Eq '(^| )(commit|run|cp|volume|image)( |$)' "${test_root}/docker.log"; then
  echo "前置检查意外创建了 Docker 资源" >&2
  exit 1
fi

mkdir -p "${test_root}/missing-home/.codex"
printf 'model = "test"\n' > "${test_root}/missing-home/.codex/config.toml"
if PATH="${test_root}/bin:${PATH}" \
  HOME="${test_root}/missing-home" \
  CODEX_BIN="${test_root}/codex" \
  FAKE_DOCKER_LOG="${test_root}/missing.log" \
    "${setup_script}" >/dev/null 2>&1; then
  echo "缺少 auth.json 时前置检查仍然通过" >&2
  exit 1
fi

echo "Codex 临时容器前置检查：通过"