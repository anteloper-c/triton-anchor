"""
Unified TTIR Pipeline
======================

Extracts the 7 mandatory TTIR optimization passes that are 100% shared
across all three projects (spine-triton, triton_race, fantasy-triton).

This is a **core invariant** — the pass list is append-only and
synchronized with upstream Triton.

The pipeline also supports conditional passes controlled by ``HWCapability``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .hw_capability import HWCapability


# ── Pass-module loading & probing caches ───────────────────────────────
# libtriton's pass modules and the availability of a given pass never change
# within a process, so the per-compile ``import`` + ``getattr`` probes are
# pure overhead on the pipeline-construction hot path.  Both are resolved
# once here and replayed for every subsequent kernel compilation.

_pass_modules: Dict[str, object] = {}
_pass_probe_cache: Dict[Tuple[str, str], Optional[Callable]] = {}

# Resolved pass plans — mandatory plan is process-wide; conditional plans
# are keyed by (is_gpgpu, enable_loop_unroll).
_mandatory_plan_cache: Optional[Tuple[Callable, ...]] = None
_conditional_plan_cache: Dict[Tuple[bool, bool], Tuple[Optional[Callable], ...]] = {}


def _load_pass_module():
    """Load (and cache) ``triton._C.libtriton.passes``."""
    mod = _pass_modules.get("passes")
    if mod is None:
        from triton._C.libtriton import passes as mod

        _pass_modules["passes"] = mod
    return mod


def _probe_pass(module, pass_name: str) -> Optional[Callable]:
    """Probe (and cache) whether ``module`` exposes ``pass_name``.

    Returns:
        The bound pass function, or ``None`` if unavailable.
    """
    mod_name = getattr(module, "__name__", str(module))
    key = (mod_name, pass_name)
    if key not in _pass_probe_cache:
        _pass_probe_cache[key] = getattr(module, pass_name, None)
    return _pass_probe_cache[key]


def build_ttir_pipeline(pm, hw: Optional[HWCapability] = None):
    """Build the standard TTIR optimization pipeline.

    This is extracted from triton_race's ``_make_ttir()`` and is identical
    to the 7 mandatory passes used by all three projects.

    Args:
        pm: An ``mlir.PassManager`` instance.
        hw: Optional ``HWCapability``.  If provided, conditional passes
            are added based on hardware capabilities.

    Usage::

        from triton._C.libtriton import ir, passes

        mod = ...  # TTIR module
        pm = ir.pass_manager(mod.context)
        build_ttir_pipeline(pm, hw=my_hw_capability)
        pm.run(mod)

    Note:
        This function requires ``triton._C.libtriton`` to be available.
        It will raise ``ImportError`` if Triton is not installed.
    """
    # ═══════════════════════════════════════════════════════════════════
    # Mandatory Passes (7) — shared 100% across all projects
    # Order matters: inliner → combine → canonicalize → reorder → cse → licm → dce
    # ═══════════════════════════════════════════════════════════════════
    for fn in _mandatory_pass_plan():
        fn(pm)

    # ═══════════════════════════════════════════════════════════════════
    # Conditional Passes — controlled by HWCapability
    # ═══════════════════════════════════════════════════════════════════
    if hw is not None:
        for fn in _conditional_pass_plan(hw):
            if fn is not None:
                fn(pm)


def _mandatory_pass_plan() -> Tuple[Callable, ...]:
    """Resolve (once per process) the 7 mandatory pass functions.

    Returns:
        Bound pass functions in execution order:
        inliner → combine → canonicalize → reorder → cse → licm → dce.
    """
    global _mandatory_plan_cache
    if _mandatory_plan_cache is None:
        passes = _load_pass_module()
        _mandatory_plan_cache = (
            passes.common.add_inliner,
            passes.ttir.add_combine,
            passes.common.add_canonicalizer,
            passes.ttir.add_reorder_broadcast,
            passes.common.add_cse,
            passes.common.add_licm,
            passes.common.add_symbol_dce,
        )
    return _mandatory_plan_cache


def _conditional_pass_plan(hw: HWCapability) -> Tuple[Optional[Callable], ...]:
    """Resolve (cached per HW config) the conditional pass functions.

    The plan depends only on (compute_paradigm, enable_loop_unroll), so it
    is cached per configuration — subsequent kernel compilations replay the
    resolved pass list without re-probing the pass modules.

    A ``None`` entry marks an optional pass that is unavailable in this
    build; required passes raise here on first resolution instead.

    Returns:
        Bound pass functions (or ``None`` for unavailable optional passes).
    """
    from .hw_capability import ComputeParadigm

    key = (hw.compute_paradigm == ComputeParadigm.GPGPU, bool(hw.enable_loop_unroll))
    plan = _conditional_plan_cache.get(key)
    if plan is None:
        passes = _load_pass_module()

        steps: List[Optional[Callable]] = []
        # GPU path needs tensor pointer rewriting (CRITICAL — must not silently skip)
        if key[0]:
            steps.append(_require_resolve(passes.ttir, "add_rewrite_tensor_pointer"))
        # Optional loop unrolling (safe to skip if unavailable)
        if key[1]:
            steps.append(_probe_pass(passes.ttir, "add_loop_unroll"))
        # FlagTree extra optimization (optional, auto-probe)
        steps.append(_probe_pass(passes.ttir, "add_expression_restructing"))

        plan = tuple(steps)
        _conditional_plan_cache[key] = plan
    return plan


def _require_resolve(module, pass_name: str) -> Callable:
    """Resolve a critical-path pass. Raise if not available.

    For passes on the critical compilation path (e.g., GPU's
    add_rewrite_tensor_pointer) whose absence would cause incorrect
    compilation results.
    """
    fn = _probe_pass(module, pass_name)
    if fn is None:
        mod_name = getattr(module, "__name__", str(module))
        raise RuntimeError(
            f"Required pass '{pass_name}' not found in module '{mod_name}'. "
            f"This pass is critical for the current compilation path. "
            f"Check your Triton version and backend installation."
        )
    return fn


def _try_add_pass(module, pass_name, pm, **kwargs):
    """Safely try to add a pass. Silently skip if not available.

    For optional passes (e.g., add_expression_restructing, add_loop_unroll)
    whose absence does not affect compilation correctness.
    """
    fn = _probe_pass(module, pass_name)
    if fn is not None:
        fn(pm, **kwargs) if kwargs else fn(pm)
        return True
    return False


def _require_pass(module, pass_name, pm, **kwargs):
    """Add a critical-path pass. Raise if not available.

    For passes on the critical compilation path (e.g., GPU's
    add_rewrite_tensor_pointer, add_convert_to_ttgpuir) whose
    absence would cause incorrect compilation results.
    """
    fn = _require_resolve(module, pass_name)
    fn(pm, **kwargs) if kwargs else fn(pm)
    return True


def make_ttir(mod, metadata: dict, hw: Optional[HWCapability] = None):
    """Convenience function: build pipeline + run it on a module.

    This mirrors the signature of triton_race's ``_make_ttir(mod, metadata, options)``.

    Args:
        mod: An MLIR module (``ir.Module``).
        metadata: Compilation metadata dict (mutated in-place).
        hw: Optional ``HWCapability``.

    Returns:
        The optimized MLIR module (same object, mutated in-place).
    """
    from triton._C.libtriton import ir

    pm = ir.pass_manager(mod.context)
    pm.enable_debug()
    build_ttir_pipeline(pm, hw=hw)
    pm.run(mod)
    return mod


def inject_hw_attributes(mod, hw: HWCapability, metadata: dict):
    """将硬件能力信息注入 MLIR module 属性和编译元数据中。

    在 TTIR 优化之后、硬件感知 IR 降级之前调用。
    后端插件可通过 ``on_ttir_ready()`` hook 注入额外属性。

    Args:
        mod: MLIR module。
        hw: 目标硬件能力描述。
        metadata: 编译元数据 dict（就地更新）。
    """
    try:
        from triton._C.libtriton import ir

        builder = ir.builder(mod.context)

        # 整型硬件属性注入到 MLIR module（供下游 C++ pass 使用）
        if hw.arch_family == "riscv" and hw.matrix_cap:
            mod.set_attr("hw.num_threads", builder.get_int32_attr(hw.num_cores))
        elif hw.arch_family == "tpu" and hw.tensor_cap:
            mod.set_attr("hw.core_num", builder.get_int32_attr(hw.tensor_cap.num_cores))
        elif hw.arch_family == "gpu" and hw.gpgpu_cap:
            mod.set_attr(
                "hw.num_warps",
                builder.get_int32_attr(hw.gpgpu_cap.num_warps),
            )

    except ImportError:
        pass

    # 硬件描述信息通过 metadata dict 传递给下游 Python 代码
    metadata["hw_name"] = hw.name
    metadata["hw_paradigm"] = hw.compute_paradigm.value
    metadata["hw_arch_family"] = hw.arch_family
