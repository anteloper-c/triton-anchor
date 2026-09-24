"""
IR Text Serialization — single entry point
===========================================

Historically every compile stage serialized the MLIR module to text on its
own (``str(mod)``): the linalg adapter did it twice per ``convert()`` (once
for the allow_reorder fixup, once for kernel-name extraction) and
``validate_output()`` added a third pass.  Module → text serialization walks
the entire IR, so on large kernels these redundant round-trips showed up as
a measurable share of frontend compile time.

This module centralizes serialization so that each stage shares **one**
text snapshot per compilation instead of re-printing the module.

Note: the text is a point-in-time snapshot.  After a pass pipeline mutates
the module you must re-serialize — callers hold the responsibility of
re-snapshotting between pipeline runs, not inside one.
"""

from __future__ import annotations

import re
from typing import Any

# Matches the Triton kernel function name in serialized TTIR:
#   tt.func public @kernel_name(...)
_KERNEL_NAME_PATTERN = re.compile(r"tt\.func\s+(?:public\s+)?@(\w+)\(")


def serialize_module(mod: Any) -> str:
    """Serialize an MLIR module (or pass through text) to its textual form.

    Args:
        mod: An ``ir.Module``-like object, or an already-serialized ``str``.

    Returns:
        The MLIR text.  For ``str`` input this is the identity function,
        which lets in-process (module object) and out-of-process (text)
        callers share the same code path.
    """
    return mod if isinstance(mod, str) else str(mod)


def extract_kernel_name(ir_text: str) -> str:
    """Extract the Triton kernel function name from serialized IR text.

    Operates on text (not on the module) so the caller can reuse a single
    serialization snapshot for fixups, name extraction and validation.

    Args:
        ir_text: Serialized TTIR/MLIR module text.

    Returns:
        The kernel name, or ``""`` if not exactly one ``tt.func`` is found
        (mirrors the historical adapter behavior of refusing to guess when
        multiple kernels are present).
    """
    matches = _KERNEL_NAME_PATTERN.findall(ir_text)
    if len(matches) == 1:
        return matches[0]
    return ""
