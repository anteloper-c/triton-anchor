#!/usr/bin/env bash
# Trusted image build helper. Inputs must come from the host profile recipe.
set -euo pipefail
revision="${1:?exact LLVM revision is required}"
destination="${2:?LLVM destination is required}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'LLVM revision must be 40 lowercase hex characters' >&2; exit 2; }
[[ "$destination" == /opt/llvm ]] || { echo 'Only the image-owned /opt/llvm destination is supported' >&2; exit 2; }
mkdir -p /opt/llvm-source
git -C /opt/llvm-source init
git -C /opt/llvm-source remote add origin https://github.com/llvm/llvm-project.git
git -C /opt/llvm-source fetch --depth=1 origin "$revision"
git -C /opt/llvm-source checkout --detach FETCH_HEAD
[[ "$(git -C /opt/llvm-source rev-parse HEAD)" == "$revision" ]]
cmake -S /opt/llvm-source/llvm -B /opt/llvm-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$destination" \
  -DLLVM_ENABLE_PROJECTS=mlir -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="${LLVM_TARGETS_TO_BUILD:-host;NVPTX;AMDGPU}" \
  -DLLVM_INSTALL_UTILS=ON -DMLIR_ENABLE_BINDINGS_PYTHON=OFF
cmake --build /opt/llvm-build --parallel "${LLVM_BUILD_JOBS:-4}"
cmake --install /opt/llvm-build
printf '%s\n' "$revision" > "$destination/anchor-ci-llvm-revision"
test -x "$destination/bin/mlir-opt"
test -f "$destination/lib/cmake/mlir/MLIRConfig.cmake"
