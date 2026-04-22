#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="${ROOT_DIR}/reuleauxcoder-agent"
OUT_DIR="${ROOT_DIR}/artifacts/remote"

if [[ ! -d "${AGENT_DIR}" ]]; then
  echo "[error] agent dir not found: ${AGENT_DIR}" >&2
  exit 1
fi

build_one() {
  local goos="$1"
  local goarch="$2"
  local ext="$3"

  local target_dir="${OUT_DIR}/${goos}/${goarch}"
  local target_bin="${target_dir}/rcoder-peer${ext}"

  mkdir -p "${target_dir}"
  echo "[build] ${goos}/${goarch} -> ${target_bin}"
  (cd "${AGENT_DIR}" && CGO_ENABLED=0 GOOS="${goos}" GOARCH="${goarch}" go build -o "${target_bin}" ./cmd/reuleauxcoder-agent)
}

echo "[info] output dir: ${OUT_DIR}"

build_one linux amd64 ""
build_one linux arm64 ""
build_one darwin amd64 ""
build_one darwin arm64 ""
build_one windows amd64 ".exe"
build_one windows arm64 ".exe"

echo "[done] built peer artifacts:"
find "${OUT_DIR}" -type f -name "rcoder-peer*" -print | sort
