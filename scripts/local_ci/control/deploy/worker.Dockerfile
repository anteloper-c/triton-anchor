# Persistent local acceptance worker; production LLVM/backend recipes are separate.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ARG APT_MIRROR=http://archive.ubuntu.com/ubuntu
RUN sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
      python3 python3-venv python3-pip git cmake ninja-build build-essential \
      nodejs npm curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/ci-venv \
    && /opt/ci-venv/bin/python -m pip install --no-cache-dir \
      build setuptools wheel pybind11 pytest jsonschema pyyaml
RUN npm install -g @openai/codex@0.153.4
# Ubuntu 24.04 images can already contain an unused uid 1000 account. Replace
# that image-owned account while retaining gid 1000 as the shared workspace group.
RUN if getent passwd 1000 >/dev/null; then userdel "$(getent passwd 1000 | cut -d: -f1)"; fi \
    && if ! getent group 1000 >/dev/null; then groupadd --gid 1000 ci; fi \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash build \
    && useradd --uid 1001 --gid 1000 --create-home --shell /bin/bash agent \
    && install -d -m 0700 -o 1001 -g 1000 /home/agent/.codex \
    && install -d -m 0700 -o 1000 -g 1000 /tmp/anchor-ci-processes-1000 \
    && install -d -m 0700 -o 1001 -g 1000 /tmp/anchor-ci-processes-1001 \
    && install -d -m 2775 -o 1000 -g 1000 /workspace \
    && install -d -m 0755 /opt/anchor-ci
# System Python, the shared base venv, Node and Codex stay root-owned so tests
# cannot replace the interpreter or packages used by the isolated AI process.
ENV PATH="/opt/ci-venv/bin:${PATH}"
WORKDIR /workspace
CMD ["sleep", "infinity"]
