# Read-only Host Toolchains

LLVM and PPL may be kept in versioned directories owned by the CI account rather
than copied into image layers. Image validation, task containers and the runtime
probe mount these directories read-only at fixed paths under
`/opt/local-ci/runtime/deps`. Frontend/backend sources and writable build output
remain in image/task storage. Management recovery mounts only task/session
volumes so missing toolchains do not prevent evidence export or cleanup.

Configure a dedicated absolute `dependency_root` outside control, state and
credential directories. Each mount source must be a directory strictly beneath
that root. Directories/files must belong to the CI account, be readable by mapped
container UIDs, and have no group/other write permission. Links must be relative,
resolve inside the same version directory, and have existing targets.

Example profile fragment (replace paths, commits and digests with actual values):

```json
{
  "llvm": {"mode": "mount", "commit": "<exact LLVM commit>"},
  "mounts": [
    {
      "source": "/home/jiwang_ci/local_ci/workspace/dependencies/llvm-<commit>",
      "target": "/opt/local-ci/runtime/deps/llvm-<commit>",
      "read_only": true,
      "sha256": "<tree_digest of source>"
    },
    {
      "source": "/home/jiwang_ci/local_ci/workspace/dependencies/ppl-<version>",
      "target": "/opt/local-ci/runtime/deps/ppl",
      "read_only": true,
      "sha256": "<tree_digest of source>"
    }
  ]
}
```

Calculate each checksum using
`ops_maint.artifacts.tree_digest(Path(source))` from the trusted control code,
after finalizing permissions and internal links. This is a directory-content
digest, not an archive checksum. Image preparation, task acquisition/resumption
and deployment preflight reject mismatches. Record vendor package provenance
separately. Container read-only mounting does not prevent the host owner from
changing files; never edit a version directory used by a validated release.
Create a new version directory, rotate the image and repeat runtime preflight
instead. Old version directories must remain available to their active tasks.

Mounted dependencies are absent during `docker build`. Image
`prepare_commands` may create links/mountpoint directories but must not compile
against or inspect mounted toolchains. The normal post-build validation containers
have the mounts available. Base-image Python/system dependencies must still match
the toolchain's ABI.

Only fixed read-only directory binds are accepted; recursive nested mounts are
disabled. This is not a general host-mount mechanism and does not expose host
credentials, Docker sockets, task roots or the whole home directory.
