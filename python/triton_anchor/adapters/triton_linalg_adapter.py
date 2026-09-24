"""
TritonLinalgAdapter — In-Process Adapter wrapping triton-linalg
================================================================

This adapter wraps the triton-linalg conversion pipeline (from Cambricon)
that is used by triton_race for Sophgo TPU support.

It calls the MLIR PassManager directly (in-process), with zero subprocess
overhead.  The pass sequence is extracted from triton_race's ``_make_raceir()``.

Dependencies:
  - ``triton._C.libtriton`` must be available (i.e., triton_race installed)
  - race passes must be linked into libtriton.so

Output dialects:
  linalg, linalg_ext, tensor, memref, arith, math, scf, func, aux
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, List

from ..ir_text import extract_kernel_name, serialize_module
from .base import ILinalgPybindAdapter, AdapterConversionError

logger = logging.getLogger(__name__)

# ── Pass pipeline resolution cache ─────────────────────────────────────
# Bound pass functions live for the whole process, so resolving them once
# and replaying removes the per-conversion import + attribute-lookup cost
# from the pass-registration hot path.  ``None`` until first resolved.

_pipeline_cache = None


def _resolve_pipeline():
    """Resolve (once per process) the bound pass functions of the pipeline.

    Returns:
        A tuple of callables, each taking a pass manager and registering
        one pipeline step, in execution order.
    """
    global _pipeline_cache
    if _pipeline_cache is not None:
        return _pipeline_cache

    from triton._C.libtriton.anchor.anchor_passes import triton_to_linalg as tl
    from triton._C.libtriton.passes import common

    # Note: triton_to_ppl has been stripped. The backend should handle it if needed.
    _pipeline_cache = (
        tl.add_wrap_func_body_with_single_block,
        common.add_inliner,
        common.add_canonicalizer,
        tl.add_canonicalize_triton,
        tl.add_pointer_strength_reduction,
        common.add_canonicalizer,
        tl.add_triton_to_linalg,
        tl.add_extract_like_move_backward,
        common.add_canonicalizer,
        tl.add_arith_to_linalg,
        tl.add_math_to_linalg,
        common.add_cse,
        common.add_licm,
        tl.add_wrap_func_body_with_single_block,
    )
    return _pipeline_cache


class TritonLinalgAdapter(ILinalgPybindAdapter):
    """In-process adapter using triton-linalg (AxisInfo pointer analysis).

    This adapter directly calls the MLIR passes from triton-linalg via
    pybind11 bindings, making it the fastest conversion path.

    Note: The "triton-linalg" name is the Adapter registry name. The
    actual passes wrapped here are triton_race's self-developed 11-pass
    pipeline (``passes.race.triton_to_linalg.*``), NOT the Cambricon
    triton-linalg standalone library.

    Pass pipeline (from triton_race ``_make_raceir()``):
      1. triton_to_ppl                    — PPL index preparation
      2. wrap_func_body_with_single_block  — normalize function body
      3. inliner                           — inline called functions
      4. canonicalizer                     — standard canonicalization
      5. canonicalize_triton               — Triton-specific canonicalization
      6. pointer_strength_reduction        — pointer analysis (AxisInfo)
      7. canonicalizer                     — re-canonicalize after pointer analysis
      8. triton_to_linalg                  — core Triton→Linalg conversion
      9. extract_like_move_backward        — optimization on extract ops
      10. canonicalizer                    — post-conversion canonicalization
      11. arith_to_linalg                  — arithmetic op lowering
      12. math_to_linalg                   — math op lowering
      13. cse                              — common sub-expression elimination
      14. licm                             — loop-invariant code motion
      15. wrap_func_body_with_single_block — final normalization
    """

    def name(self) -> str:
        return "triton-linalg"

    def convert(self, ttir_module: Any, metadata: dict, context: Any = None) -> Any:
        """Convert TTIR to Linalg using triton-linalg passes.

        Args:
            ttir_module: MLIR module (``ir.Module``) after TTIR optimization.
            metadata: Compilation metadata dict.
            context: MLIR context (unused — context is obtained from module).

        Returns:
            The converted MLIR module (same object, mutated in-place).

        Raises:
            AdapterConversionError: If any pass in the pipeline fails.
        """
        try:
            from triton._C.libtriton.anchor import anchor_passes as passes
            from triton._C.libtriton import ir
        except ImportError:
            raise AdapterConversionError(
                self.name(),
                detail="triton_anchor._C not available. Is the C++ extension built?",
            )

        # Check that anchor passes are available
        if not hasattr(passes, "triton_to_linalg"):
            raise AdapterConversionError(
                self.name(), detail="anchor_passes.triton_to_linalg not available."
            )

        # ── Single IR serialization snapshot ─────────────────────────
        # One str(module) walk is shared by the allow_reorder fixup check
        # and kernel-name extraction (previously two full serializations).
        ttir_code = serialize_module(ttir_module)
        if "allow_reorder" in ttir_code and "allow_reorder = true" not in ttir_code:
            # This is a known quirk in triton_race
            logger.debug("Applying allow_reorder attribute fixup")

        # Extract kernel name for diagnostics (reuses the same snapshot)
        kernel_name = extract_kernel_name(ttir_code)
        if kernel_name:
            metadata.setdefault("name", kernel_name)

        # Build and run the pass pipeline
        pm = ir.pass_manager(ttir_module.context)
        pm.enable_debug()

        self._add_passes(pm, passes)

        try:
            pm.run(ttir_module)
        except Exception as e:
            logger.error(
                f"TritonLinalgAdapter conversion failed for kernel "
                f"'{metadata.get('name', '<unknown>')}'"
            )
            traceback.print_exc()
            raise AdapterConversionError(
                self.name(), kernel_name=metadata.get("name", ""), detail=str(e)
            )

        return ttir_module

    def _add_passes(self, pm, passes) -> None:
        """Add the triton-linalg conversion pass pipeline.

        The pass functions are resolved once per process (``_resolve_pipeline``)
        and replayed here — repeated conversions skip the per-call import
        machinery and attribute lookups on the pass-registration hot path.
        """
        for fn in _resolve_pipeline():
            fn(pm)

    def _extract_kernel_name(self, mod) -> str:
        """Extract the Triton kernel function name from the module.

        Kept for backward compatibility — operates on a fresh serialization
        only when the caller cannot provide one.
        """
        return extract_kernel_name(str(mod))

    def get_required_passes(self) -> List[str]:
        return [
            "triton_to_ppl",
            "wrap_func_body_with_single_block",
            "inliner",
            "canonicalizer",
            "canonicalize_triton",
            "pointer_strength_reduction",
            "triton_to_linalg",
            "extract_like_move_backward",
            "arith_to_linalg",
            "math_to_linalg",
            "cse",
            "licm",
        ]

    def get_output_dialects(self) -> List[str]:
        return [
            "linalg",
            "linalg_ext",
            "tensor",
            "memref",
            "arith",
            "math",
            "math_ext",
            "scf",
            "func",
            "aux",
        ]
