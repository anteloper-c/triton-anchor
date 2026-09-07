#!/usr/bin/env bash
# The supervisor supplies the trusted profile and an already prepared checkout.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${LOCAL_CI_TOOL_PYTHON:-python3}" "${script_dir}/run_tool.py" "$@"
