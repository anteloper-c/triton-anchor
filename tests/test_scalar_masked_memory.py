"""Check scalar memory effects in the actual lowered IR.

Run with the locally built triton-anchor Python package:
    python tests/test_scalar_masked_memory.py
Alternatively, set TRITON_SHARED_OPT_PATH to a tool built from this checkout.
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


def lower(source, pipeline):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scalar.mlir"
        path.write_text(source, encoding="utf-8")
        tool = os.environ.get("TRITON_SHARED_OPT_PATH")
        if tool:
            result = subprocess.run(
                [tool, str(path), *[f"--{name}" for name in pipeline]],
                check=True, capture_output=True, text=True,
            )
            return result.stdout

        from triton._C.libtriton import anchor, ir

        context = ir.context()
        ir.load_dialects(context)
        anchor.load_dialects(context)
        module = ir.parse_mlir_module(str(path), context)
        manager = ir.pass_manager(context)
        for name in pipeline:
            getattr(anchor.passes, f"add_{name.replace('-', '_')}")(manager)
        manager.run(module)
        return str(module)


def make_source(unstructured, element_type, masked=True, other=True):
    mask_operand = ", %mask" if masked else ""
    other_operand = ", %other" if masked and other else ""
    if unstructured:
        mask_clause = " mask = %mask" if masked else ""
        other_clause = " default = %other" if masked and other else ""
        body = f"""
    %offset = arith.constant 0 : i32
    %value = tts.gather %ptr[%offset]{mask_clause}{other_clause}
        : (!tt.ptr<{element_type}>, i32) -> {element_type}
    tts.scatter %value into %out[%offset]{mask_clause}
        : {element_type} into (!tt.ptr<{element_type}>, i32)
"""
    else:
        body = f"""
    %value = tt.load %ptr{mask_operand}{other_operand} : !tt.ptr<{element_type}>
    tt.store %out, %value{mask_operand} : !tt.ptr<{element_type}>
"""
    return f"""module {{
  tt.func @scalar(%ptr: !tt.ptr<{element_type}>, %out: !tt.ptr<{element_type}>,
                  %mask: i1, %other: {element_type}) -> {element_type} {{
{body}
    tt.return %value : {element_type}
  }}
}}
"""


def memory_regions(text):
    """Record the enclosing conditional regions of each memory access."""
    regions = []
    accesses = []
    for line in text.splitlines():
        condition = re.search(r"\bscf\.if\s+(%[\w]+)", line)
        if condition:
            regions.append((condition.group(1), "then"))
        elif re.match(r"\s*}\s*else\s*{", line):
            regions[-1] = (regions[-1][0], "else")
        elif re.match(r"\s*}", line) and regions:
            regions.pop()
        access = re.search(r"\b(?:affine|memref)\.(load|store)\b", line)
        if access:
            accesses.append((access.group(1), tuple(regions)))
    return accesses


class ScalarMaskedMemoryTests(unittest.TestCase):
    def check_case(self, unstructured, element_type, masked=True, other=True):
        pipeline = ("unstructured-to-memref",) if unstructured else ("triton-to-linalg",)
        output = lower(make_source(unstructured, element_type, masked, other), pipeline)
        self.assertNotRegex(output, r"\b(?:tt\.(?:load|store)|tts\.(?:gather|scatter))\b")
        accesses = memory_regions(output)
        self.assertCountEqual([kind for kind, _ in accesses], ["load", "store"])
        if not masked:
            self.assertTrue(all(not regions for _, regions in accesses), output)
            self.assertNotIn("scf.if", output)
            return

        # Find the runtime mask after SSA names have been rewritten.
        mask = re.search(r"(%[\w]+): i1\b", output).group(1)
        for _, regions in accesses:
            self.assertIn((mask, "then"), regions, output)
            self.assertFalse(any(branch == "else" for _, branch in regions), output)

        # A false mask yields the explicit fallback or a typed zero.
        else_region = re.search(r"\belse\s*{([^{}]*)}", output)
        self.assertIsNotNone(else_region, output)
        yielded = re.search(
            rf"scf\.yield (%[\w]+) : {element_type}\b", else_region.group(1)
        )
        self.assertIsNotNone(yielded, output)
        fallback = yielded.group(1)
        if other:
            expected = re.search(rf"(%[\w]+): {element_type}\b", output).group(1)
            self.assertEqual(fallback, expected, output)
        else:
            self.assertRegex(
                output,
                rf"{re.escape(fallback)} = arith\.constant "
                rf"(?:0(?:\.0+[eE][+-]0+)?|false) : {element_type}\b",
            )

    def test_masked_with_other(self):
        for unstructured in (False, True):
            for element_type in ("i32", "f16", "f32"):
                with self.subTest(unstructured=unstructured, element_type=element_type):
                    self.check_case(unstructured, element_type)

    def test_masked_without_other(self):
        for unstructured in (False, True):
            for element_type in ("i32", "f16", "f32"):
                with self.subTest(unstructured=unstructured, element_type=element_type):
                    self.check_case(unstructured, element_type, other=False)

    def test_unmasked(self):
        for unstructured in (False, True):
            with self.subTest(unstructured=unstructured):
                self.check_case(unstructured, "f32", masked=False)


if __name__ == "__main__":
    unittest.main()
