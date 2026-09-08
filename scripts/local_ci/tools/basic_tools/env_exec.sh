#!/usr/bin/env bash
# Environment scripts are supplied by the trusted profile, never by agent parameters.
set -eo pipefail
setup_script="${1:?environment script required}"
shift
setup_args=()
while [[ "$#" -gt 0 && "$1" != "--" ]]; do
  setup_args+=("$1")
  shift
done
[[ "$#" -gt 1 && "$1" == "--" ]] || exit 2
shift
task_tmp="${TMPDIR:-}"
task_cache="${TRITON_CACHE_DIR:-}"
task_dump="${TRITON_DUMP_DIR:-}"
source "${setup_script}" "${setup_args[@]}"
export TMPDIR="${task_tmp}" TRITON_CACHE_DIR="${task_cache}" TRITON_DUMP_DIR="${task_dump}"
exec "$@"
