#!/usr/bin/env bash
set -euo pipefail

usage="prepare_codex_checkout.sh <repo-url> <branch> <workspace-root> <name> <target-sha> [base-branch] [base-sha]"
repo_url="${1:?usage: ${usage}}"
branch="${2:?usage: ${usage}}"
workspace_root="${3:?usage: ${usage}}"
checkout_name="${4:?usage: ${usage}}"
target_sha="${5:?usage: ${usage}}"
base_branch="${6:-}"
base_sha="${7:-}"

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
if [[ -n "${base_branch}" ]] && ! git check-ref-format --branch "${base_branch}" >/dev/null 2>&1; then
  echo "Invalid Codex base branch: ${base_branch}" >&2
  exit 1
fi
if [[ -n "${base_sha}" && ! "${base_sha}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Invalid Codex base SHA: ${base_sha}" >&2
  exit 1
fi
if [[ -n "${base_branch}" && -z "${base_sha}" ]]; then
  echo "Codex base branch requires an exact base SHA" >&2
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

if [[ -n "${base_branch}" ]]; then
  if ! git -C "${checkout_dir}" fetch --quiet --no-tags gitee \
    "+refs/heads/${base_branch}:refs/codex/base"; then
    echo "Failed to fetch Codex base branch ${base_branch}" >&2
    exit 1
  fi
  fetched_base_sha="$(git -C "${checkout_dir}" rev-parse refs/codex/base 2>/dev/null || true)"
  if [[ "${fetched_base_sha}" != "${base_sha}" ]]; then
    echo "Codex base SHA mismatch: expected ${base_sha}, got ${fetched_base_sha:-unavailable}" >&2
    exit 1
  fi
elif [[ -n "${base_sha}" ]] &&
  ! git -C "${checkout_dir}" cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
  # A previous push SHA is normally present in the target branch history. A
  # best-effort direct fetch also preserves useful comparisons after a force push.
  git -C "${checkout_dir}" fetch --quiet --no-tags gitee "${base_sha}" >/dev/null 2>&1 || true
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
