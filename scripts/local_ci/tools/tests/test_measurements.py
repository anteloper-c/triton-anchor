"""Tool boundary tests; subprocess fakes replace expensive builds, never the dispatcher."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ir_benchmark = load(
    "local_ci_tool_ir_benchmark",
    ROOT / "tools/basic_tools/performance/ir_serialization_benchmark.py",
)
profile_compare = load(
    "local_ci_tool_profile_compare",
    ROOT / "tools/basic_tools/performance/compare_pass_profile.py",
)
profile_benchmark = load(
    "local_ci_tool_profile_benchmark",
    ROOT / "tools/basic_tools/performance/pass_profile_benchmark.py",
)
compile_benchmark = load(
    "local_ci_tool_compile_benchmark",
    ROOT / "tools/basic_tools/performance/compile_benchmark.py",
)


class MeasurementValidationTests(unittest.TestCase):
    def test_profile_compare_rejects_missing_candidate_passes(self):
        with self.assertRaises(ValueError):
            profile_compare.compare(
                None,
                {"summary": {"add": {"passes": {}}}},
                ["add"],
                0.2,
                1,
                1,
                10,
                "slowdown",
                "base",
                "head",
            )

    def test_compile_benchmark_rejects_empty_or_zero_sample(self):
        for kernels, repeat in (("", 1), ("add", 0)):
            with self.assertRaises(ValueError):
                compile_benchmark.run_parent(
                    argparse.Namespace(kernels=kernels, repeat=repeat, warmup=0)
                )

    def test_pass_profile_no_events_is_an_execution_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            args = argparse.Namespace(
                kernels="add",
                repeat=1,
                warmup=0,
                top_n=10,
                flaggems_root=directory,
                cache_root=str(root / "benchmark/cache"),
                backend="fixture",
                keep_workdirs=False,
            )
            with patch.object(
                profile_benchmark, "run_child", return_value=({"compile_est_ms": 1}, [])
            ):
                with self.assertRaisesRegex(RuntimeError, "No MLIR pass timing"):
                    profile_benchmark.run_parent(args)

    def test_roundtrip_checks_canonical_content_and_verifier(self):
        class Module:
            def __init__(self, text, valid=True):
                self.text, self.valid = text, valid

            def __str__(self):
                return self.text

            def verify(self):
                return self.valid

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.ttir"
            ir = types.SimpleNamespace(
                parse_mlir_module=lambda p, c: Module(Path(p).read_text())
            )
            binding = types.ModuleType("triton._C.libtriton")
            binding.ir = ir
            with patch.dict(sys.modules, {"triton._C.libtriton": binding}):
                row = ir_benchmark.measure_once(
                    [Module("module {}\n")], None, [path], "add", "repeat", 0
                )
                self.assertTrue(row["roundtrip_verified"])
                ir.parse_mlir_module = lambda p, c: Module("module { changed }\n")
                with self.assertRaisesRegex(RuntimeError, "canonical"):
                    ir_benchmark.measure_once(
                        [Module("module {}\n")], None, [path], "add", "repeat", 0
                    )
                ir.parse_mlir_module = lambda p, c: Module("module {}\n", False)
                with self.assertRaisesRegex(RuntimeError, "invalid MLIR"):
                    ir_benchmark.measure_once(
                        [Module("module {}\n")], None, [path], "add", "repeat", 0
                    )
