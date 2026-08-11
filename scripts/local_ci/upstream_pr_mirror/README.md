# Upstream PR Mirror

This module mirrors selected open pull requests from `RACE-org/triton-anchor`
into the fork so trusted CI can review exact upstream base and head commits.

It is stored under `scripts/local_ci/` as part of the trusted CI control plane,
but it runs on the GitHub-hosted runner through
`.github/workflows/mirror-upstream-prs.yml`. It is not executed by the local
Sophgo runner or `poll_gitee_and_run.sh`.

The canonical entrypoint is `mirror_upstream_prs.py`; its focused tests live in
`tests/`.
