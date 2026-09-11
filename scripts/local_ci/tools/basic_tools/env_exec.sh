#!/usr/bin/env bash
# Load a profile recipe while preserving this task's paths and resource budget.
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
declare -A task_values=()
for name in HOME TMPDIR TRITON_CACHE_DIR TRITON_DUMP_DIR XDG_CACHE_HOME ANCHOR_DIR BACKEND_PATH PYTHON_BIN PYTHON_VENV_ACTIVATE MAX_JOBS CMAKE_BUILD_PARALLEL_LEVEL NINJAFLAGS BASELINE_JSON; do
  if [[ -v "$name" ]]; then task_values["$name"]="${!name}"; fi
done
while IFS= read -r name; do
  task_values["$name"]="${!name}"
done < <(compgen -e | sed -n '/^LOCAL_CI_/p')
source "${setup_script}" "${setup_args[@]}"
for name in "${!task_values[@]}"; do
  printf -v "$name" '%s' "${task_values[$name]}"
  export "$name"
done
if [[ "${PYTHON_BIN:-}" == /* ]]; then
  export PATH="$(dirname "$PYTHON_BIN"):$PATH"
  export VIRTUAL_ENV="$(dirname "$(dirname "$PYTHON_BIN")")"
fi
# Keep absolute SDK dependency paths, excluding product source shadowing.
clean_pythonpath=""
IFS=: read -r -a pythonpaths <<< "${PYTHONPATH:-}"
for entry in "${pythonpaths[@]}"; do
  if [[ "$entry" == /* && "$entry" != "${ANCHOR_DIR:-}" && "$entry" != "${ANCHOR_DIR:-}/"* ]]; then
    clean_pythonpath="${clean_pythonpath:+$clean_pythonpath:}$entry"
  fi
done
export PYTHONPATH="$clean_pythonpath"
exec "$@"
