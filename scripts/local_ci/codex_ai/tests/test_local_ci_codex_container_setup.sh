#!/usr/bin/env bash
# Legacy container execution was removed; verify the old entry cannot execute.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set +e
output="$(bash "${ROOT}/run_codex_ai_ci.sh" 2>&1)"
status=$?
set -e
[[ "${status}" == 2 ]]
[[ "${output}" == *'legacy execution entry is disabled'* ]]
