#!/usr/bin/env bash
set -euo pipefail

TOOLS_DIR="${1:?usage: bootstrap_tools.sh <tools-dir> [all|osv]}"
MODE="${2:-all}"
if [[ "${MODE}" != "all" && "${MODE}" != "osv" ]]; then
  echo "usage: bootstrap_tools.sh <tools-dir> [all|osv]" >&2
  exit 2
fi
DOWNLOAD_DIR="${TOOLS_DIR}/downloads"
BIN_DIR="${TOOLS_DIR}/bin"
mkdir -p "${DOWNLOAD_DIR}" "${BIN_DIR}"

download_verified() {
  local url="$1"
  local output="$2"
  local expected_sha256="$3"

  if [[ ! -f "${output}" ]]; then
    curl -L --fail --retry 3 --output "${output}.partial" "${url}"
    mv "${output}.partial" "${output}"
  fi
  echo "${expected_sha256}  ${output}" | sha256sum -c -
}

if [[ "${MODE}" == "all" ]]; then
  SCANCODE_ARCHIVE="${DOWNLOAD_DIR}/scancode-toolkit-v32.5.0_py3.12-linux.tar.gz"
  SCANCODE_HOME="${TOOLS_DIR}/scancode-toolkit-v32.5.0"
  SCANCODE="${SCANCODE_HOME}/scancode"
  download_verified \
    "https://github.com/aboutcode-org/scancode-toolkit/releases/download/v32.5.0/scancode-toolkit-v32.5.0_py3.12-linux.tar.gz" \
    "${SCANCODE_ARCHIVE}" \
    "638adcd0af576d1f4d5b64dde228724b3ca4fdee2c4de20d88e4356be353f027"
  if [[ ! -x "${SCANCODE_HOME}/venv/bin/scancode" ]]; then
    if [[ ! -d "${SCANCODE_HOME}" ]]; then
      tar -xzf "${SCANCODE_ARCHIVE}" -C "${TOOLS_DIR}"
    fi
    (cd "${SCANCODE_HOME}" && CFG_QUIET=-qq ./configure)
  fi

  SYFT_ARCHIVE="${DOWNLOAD_DIR}/syft_1.51.0_linux_amd64.tar.gz"
  SYFT="${BIN_DIR}/syft"
  download_verified \
    "https://github.com/anchore/syft/releases/download/v1.51.0/syft_1.51.0_linux_amd64.tar.gz" \
    "${SYFT_ARCHIVE}" \
    "2a2e837a2c8d59ec9af5472ee22d3b04ee463c4e44476ecf993fd1e5ab6ebc7f"
  if [[ ! -x "${SYFT}" ]]; then
    tar -xzf "${SYFT_ARCHIVE}" -C "${BIN_DIR}" syft
    chmod +x "${SYFT}"
  fi
fi

OSV_SCANNER="${BIN_DIR}/osv-scanner"
download_verified \
  "https://github.com/google/osv-scanner/releases/download/v2.5.1/osv-scanner_linux_amd64" \
  "${OSV_SCANNER}" \
  "f9f25499a2c8cc367b3af45df2ea7eeca7fbccceab9c35079968f4b3652194be"
chmod +x "${OSV_SCANNER}"

if [[ "${MODE}" == "all" ]]; then
  (cd "${SCANCODE_HOME}" && "${SCANCODE}" --version)
  "${SYFT}" version
fi
"${OSV_SCANNER}" --version

{
  if [[ "${MODE}" == "all" ]]; then
    printf 'SCANCODE=%q\n' "${SCANCODE}"
    printf 'SYFT=%q\n' "${SYFT}"
  fi
  printf 'OSV_SCANNER=%q\n' "${OSV_SCANNER}"
} > "${TOOLS_DIR}/tools.env"
