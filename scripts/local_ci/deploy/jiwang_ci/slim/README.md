# Shared Slim Foundation

This recipe follows `docs/build.md` and `docker/build-env.Dockerfile`, without
privileged containers. It contains Ubuntu 24.04, Python 3.12, the compiler and
build tools, CPU Torch 2.8, the supplied Torch TPU wheel, and the Codex executable.
It does not contain LLVM, PPL, FlagGems or preinstalled Triton/backend wheels.

Populate an isolated build directory with this Dockerfile, the executable named
`codex`, and `wheels/*.whl`. Download the pinned requirements plus CPU Torch
2.8.0 from `https://download.pytorch.org/whl/cpu`, then add the supplied
`torch_tpu-0.18.0+torch2.8-cp312-cp312-linux_x86_64.whl`. Resolve duplicates before
creating `wheels/SHA256SUMS`. BuildKit reads both wheel files and the executable
through temporary build mounts; installation packages are not image layers.

The actual wheel inventory/checksums and build logs live under the deployment's
`packages/slim-foundation` and `deploy-logs` directories. APT packages follow the
Ubuntu repositories at build time; this is not a byte-reproducible APT snapshot.
The final image is pinned by immutable digest in the private profile.

`configure.py` switches only the Triton 3.0 profile after saving a private rollback
configuration. It retains the pinned frontend/backend repositories and existing
LLVM/PPL mounts, adds a verified FlagGems source mount, and removes the FlagGems
wheel install. `enable_flaggems.py` adds only a `.pth` entry for the trusted
source's `src` directory. Candidate/base venvs inherit this entry. Git trusts only
the exact read-only mount path, not every repository. Cache/output paths must
remain task-private, not inside the mount. `validate_flaggems.py` verifies source
imports, no installed distribution/C extension, and a numerical Sophgo add.

Compatible Triton branches can reuse the foundation layers while mounting their
exact LLVM revision (and PPL/FlagGems where needed). A shared base is not a shared
mutable Python environment: each task still builds its own frontend/backend
wheels and uses isolated venvs. Every version must pass its own validation.
Python ABI, C++ runtime, Torch/TPU and backend compatibility can require a separate
base or dependency profile; changing LLVM alone is not a universal guarantee.

Keep the previous validated release until the new release and formal runtime
preflight pass. Do not run global image pruning. This recipe never publishes
code, starts workers, copies secrets into images, or enables SMTP.
