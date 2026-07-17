# Pass profiling

Set `TRITON_ANCHOR_PROFILE=1` before compiling a kernel to enable MLIR
PassManager timing for every pass pipeline executed by triton-anchor:

```bash
TRITON_ANCHOR_PROFILE=1 python your_kernel.py
```

The switch is an alias for the existing `MLIR_ENABLE_TIMING=1` behavior. It
uses MLIR's `PassManager::enableTiming()` implementation and writes the timing
report to the process diagnostic stream. The report contains the elapsed time
for each pass, so the largest entry identifies the current hot pass. Repeated
passes such as `canonicalizer` appear once for each invocation.

Profiling is disabled when the variable is unset, `0`, `false`, or `off`:

```bash
TRITON_ANCHOR_PROFILE=0 python your_kernel.py
```

Accepted true values are `1`, `true`, and `on`, case-insensitively.
`MLIR_ENABLE_TIMING=1` remains supported for compatibility; setting either
variable enables the same timing implementation.

## Cache behavior

`TRITON_ANCHOR_PROFILE` participates in Triton's cache key. Enabling it after
an unprofiled compilation therefore forces a separate compilation cache entry
instead of silently returning an existing binary without running any passes.
A warm cache hit does not execute the compiler pipeline and consequently has
no pass timings to report.

For a clean one-shot profile, use a fresh Triton cache directory:

```bash
TRITON_CACHE_DIR=/tmp/triton-anchor-profile-cache \
TRITON_ANCHOR_PROFILE=1 \
python your_kernel.py 2>pass-profile.log
```

## Scope and overhead

The timer measures MLIR pass execution only. It does not represent Python JIT
overhead, adapter wrapper overhead, backend subprocess time, linking, cache
I/O, or kernel execution. Use the end-to-end compile benchmark for those
broader costs.

When profiling is enabled, reading clocks and printing the report adds a small
amount of compile-time overhead. It does not affect the generated program or
the runtime performance of an already compiled kernel. When profiling is
disabled, no MLIR timing manager is enabled.

## Local CI artifacts

The local CI runner can turn the raw timing report into structured artifacts:

- `pass-profile.json`: per-kernel summary, raw timing events, and hotspots.
- `pass-profile-events.csv`: every parsed timing row.
- `pass-profile-summary.csv`: median/mean pass time by kernel and pass.
- `pass-profile-hotspots.md`: top candidate hot passes sorted by median time.
- `pass-profile-comparison.*`: PR candidate versus base comparison.

Baselines are cached in the Gitee results branch under
`pass-profile/by-sha/<commit>/<backend-profile>/latest.json`.
