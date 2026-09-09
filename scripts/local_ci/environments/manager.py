#!/usr/bin/env python3
"""Public Rootless Docker image and task-attempt environment interface."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environments.artifacts import (
    EnvironmentError,
    SHA_RE,
    DIGEST_RE,
    NAME_RE,
    safe_source,
    absolute_path,
    atomic_json,
    fingerprint,
    file_digest,
    tree_digest,
    shared_workspace_digest,
    extract_verified_archive,
)
from environments.runtime import EnvironmentManager, docker_command, identities, main

__all__ = [
    "EnvironmentManager",
    "EnvironmentError",
    "docker_command",
    "identities",
    "SHA_RE",
    "DIGEST_RE",
    "NAME_RE",
    "safe_source",
    "absolute_path",
    "atomic_json",
    "fingerprint",
    "file_digest",
    "tree_digest",
    "shared_workspace_digest",
    "extract_verified_archive",
]
if __name__ == "__main__":
    main()
