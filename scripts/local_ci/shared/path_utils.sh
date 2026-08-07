#!/usr/bin/env bash

# Keep shell path normalization identical across the poller and deterministic runner.
safe_path_part() {
  local value="$1"
  value="${value//\//_}"
  value="$(printf '%s' "${value}" | tr -c 'A-Za-z0-9._-' '_')"
  value="${value##_}"
  value="${value%%_}"
  printf '%s' "${value:-default}"
}


write_gitee_askpass() {
  local askpass_path="$1"
  cat > "${askpass_path}" <<'SH'
#!/usr/bin/env sh
case "$1" in
  *Username*) printf '%s\n' "${GITEE_USERNAME:-likehupochuan}" ;;
  *) printf '%s\n' "${GITEE_TOKEN}" ;;
esac
SH
  chmod 700 "${askpass_path}"
}
