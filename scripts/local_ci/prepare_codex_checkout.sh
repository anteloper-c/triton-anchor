#!/usr/bin/env bash
set -euo pipefail

repo_url="${1:?usage: prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha>}"
branch="${2:?usage: prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha>}"
workspace_root="${3:?usage: prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha>}"
checkout_name="${4:?usage: prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha>}"
target_sha="${5:?usage: prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha>}"

case "${checkout_name}" in
  "" | *[!A-Za-z0-9._-]*)
    echo "Invalid Codex checkout name: ${checkout_name}" >&2
    exit 1
    ;;
esac
if ! git check-ref-format --branch "${branch}" >/dev/null 2>&1; then
  echo "Invalid Codex checkout branch: ${branch}" >&2
  exit 1
fi
if [[ ! "${target_sha}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Invalid Codex target SHA: ${target_sha}" >&2
  exit 1
fi

if ! mkdir -p "${workspace_root}" || [[ ! -w "${workspace_root}" ]]; then
  echo "Codex workspace root is not writable: ${workspace_root}" >&2
  exit 1
fi

workspace_parent="$(
  mktemp -d "${workspace_root%/}/${checkout_name}-${target_sha:0:12}.XXXXXX"
)"
checkout_dir="${workspace_parent}/checkout"
keep_workspace="false"

cleanup_failed_checkout() {
  if [[ "${keep_workspace}" != "true" && -d "${workspace_parent}" ]]; then
    rm -rf -- "${workspace_parent}"
  fi
}
trap cleanup_failed_checkout EXIT

if ! git clone --quiet \
  --origin gitee \
  --branch "${branch}" \
  --single-branch \
  --no-tags \
  --no-checkout \
  "${repo_url}" \
  "${checkout_dir}"; then
  echo "Failed to clone Codex task branch ${branch}" >&2
  exit 1
fi

if ! git -C "${checkout_dir}" cat-file -e "${target_sha}^{commit}" 2>/dev/null; then
  if ! git -C "${checkout_dir}" fetch --quiet --no-tags gitee "${target_sha}"; then
    echo "Target SHA is unavailable from ${branch}: ${target_sha}" >&2
    exit 1
  fi
fi
if ! git -C "${checkout_dir}" checkout --quiet --detach "${target_sha}"; then
  echo "Failed to check out Codex target SHA: ${target_sha}" >&2
  exit 1
fi

checkout_sha="$(git -C "${checkout_dir}" rev-parse HEAD 2>/dev/null || true)"
if [[ "${checkout_sha}" != "${target_sha}" ]]; then
  echo "Codex checkout SHA mismatch: expected ${target_sha}, got ${checkout_sha:-unavailable}" >&2
  exit 1
fi

# The Codex process gets no relay credentials or remote endpoint. Its only
# input is this verified, disposable checkout.
git -C "${checkout_dir}" remote remove gitee >/dev/null 2>&1 || true
if [[ ! -w "${checkout_dir}" || ! -w "${checkout_dir}/.git" ]]; then
  echo "Codex checkout is not writable by uid $(id -u): ${checkout_dir}" >&2
  exit 1
fi

keep_workspace="true"
printf '%s\n' "${checkout_dir}"
