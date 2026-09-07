#!/usr/bin/env bash
# Compatibility service entrypoint. Local CI v4 is always Codex-driven.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON_BIN:-python3}" "${ROOT}/agent_ci/worker.py" "$@"
