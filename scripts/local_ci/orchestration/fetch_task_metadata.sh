#!/usr/bin/env bash
set -euo pipefail

usage="fetch_task_metadata.sh <repo-url> <metadata-ref> <task-ref> <target-sha> <output-file>"
repo_url="${1:?usage: ${usage}}"
metadata_ref="${2:?usage: ${usage}}"
task_ref="${3:?usage: ${usage}}"
target_sha="${4:?usage: ${usage}}"
output_file="${5:?usage: ${usage}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
validator="${LOCAL_CI_ROOT}/shared/validate_task_metadata.py"
temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/local-ci-task-metadata.XXXXXX")"
raw_metadata="${temporary_dir}/task-metadata.json"

cleanup() {
  rm -rf -- "${temporary_dir}"
}
trap cleanup EXIT

if [[ ! "${metadata_ref}" =~ ^ci/meta/pr-[0-9]+/.+$ ]]; then
  echo "无效的 PR metadata ref：${metadata_ref}" >&2
  exit 2
fi

git -C "${temporary_dir}" init -q
git -C "${temporary_dir}" remote add origin "${repo_url}"
if ! git -C "${temporary_dir}" fetch -q --depth=1 origin \
  "refs/heads/${metadata_ref}:refs/remotes/origin/task-metadata"; then
  echo "无法获取 PR metadata ref：${metadata_ref}" >&2
  exit 1
fi
if ! git -C "${temporary_dir}" show \
  "refs/remotes/origin/task-metadata:task-metadata.json" > "${raw_metadata}"; then
  echo "PR metadata ref 中缺少 task-metadata.json：${metadata_ref}" >&2
  exit 1
fi

"${PYTHON_BIN}" "${validator}" \
  --input "${raw_metadata}" \
  --output "${output_file}" \
  --task-ref "${task_ref}" \
  --target-sha "${target_sha}"
