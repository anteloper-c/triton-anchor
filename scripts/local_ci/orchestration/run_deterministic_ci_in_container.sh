#!/usr/bin/env bash
# v3 execution retired: historical parsers remain readable, execution cannot bypass v4.
set -euo pipefail
echo 'This legacy execution entry is disabled. Use scripts/local_ci/poll_gitee_and_run.sh --config /opt/local-ci/config.json; Codex selects individual v4 tools in persistent environments.' >&2
exit 2
