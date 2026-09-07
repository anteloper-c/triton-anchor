"""Tool boundary tests; subprocess fakes replace expensive builds, never the dispatcher."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from shlex import join as shlex_join
import subprocess
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


tool = load("local_ci_independent_tools", ROOT / "tools/run_tool.py")
selector = load("local_ci_tool_flaggems_selector", ROOT / "deterministic_ci/flaggems/select_flaggems_tests.py")
sys.modules["select_flaggems_tests"] = selector
batch = load("local_ci_tool_flaggems_batch", ROOT / "deterministic_ci/flaggems/batch_test_flaggems.py")
ir_benchmark = load("local_ci_tool_ir_benchmark", ROOT / "deterministic_ci/performance/ir_serialization_benchmark.py")
profile_compare = load("local_ci_tool_profile_compare", ROOT / "deterministic_ci/performance/compare_pass_profile.py")
profile_benchmark = load("local_ci_tool_profile_benchmark", ROOT / "deterministic_ci/performance/pass_profile_benchmark.py")
compile_benchmark = load("local_ci_tool_compile_benchmark", ROOT / "deterministic_ci/performance/compile_benchmark.py")

FAKE_PYTHON = """#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:]
with open(os.environ['FAKE_CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[:2]==['-m','build']:
 p=pathlib.Path('dist'); p.mkdir(exist_ok=True); (p/os.environ.get('FAKE_WHEEL_NAME','triton_anchor-0.0.0-py3-none-any.whl')).write_bytes(b'fake wheel boundary')
elif args[:3]==['-m','pip','install']:
 sys.exit(int(os.environ.get('FAKE_INSTALL_EXIT','0')))
elif args[:2]==['-I','-c']:
 print(json.dumps({'distribution_version':'0.0.0','imports':{'triton':'/fake/site-packages/triton/__init__.py','triton_anchor':'/fake/site-packages/triton_anchor/__init__.py'}}))
elif args and pathlib.Path(args[0]).name=='batch_test_flaggems.py':
 p=pathlib.Path(args[args.index('--artifact-dir')+1]); (p/'flaggems-summary.json').write_text(json.dumps({'summary':{'status':'pass','total':1,'passed':1}}))
 pathlib.Path(args[args.index('--selected-output')+1]).write_text('fixture-selected')
elif args and pathlib.Path(args[0]).name in ('compile_benchmark.py','pass_profile_benchmark.py','ir_serialization_benchmark.py'):
 name=pathlib.Path(args[0]).name; m={'count':1,'median_ms':1.0}
 if name=='compile_benchmark.py': summary={'all_correct':True,'compile_est':m}
 elif name=='pass_profile_benchmark.py': summary={'passes':{'canonicalize':{'wall_ms':m}}}
 else: summary={'module_count':1,'metrics':{k:m for k in ('serialize','deserialize','roundtrip')}}
 d={'summary':{'add':summary},'metadata':{},'events':[{'kernel':'add','kind':'pass'}],'raw':[{'kernel':'add','roundtrip_verified':True}]}
 pathlib.Path(args[args.index('--output-json')+1]).write_text(json.dumps(d))
else:
 sys.exit(0)
"""


class ToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="independent-ci-tools-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / "placeholder").write_text("fixture")
        for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Tool Tests", "-c", "user.email=tests@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(self.checkout), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.sha = subprocess.check_output(["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True).strip()
        self.python = self.root / "fixture-python"
        self.python.write_text(FAKE_PYTHON)
        self.python.chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.env = {**os.environ, "ANCHOR_DIR": str(self.checkout), "LOCAL_CI_TASK_ROOT": str(self.root / "task"),
                    "LOCAL_CI_ARTIFACT_DIR": str(self.root / "artifacts/build"), "LOCAL_CI_TESTED_SHA": self.sha,
                    "PYTHON_BIN": str(self.python), "LOCAL_CI_TOOL_PYTHON": sys.executable, "PACKAGE_TOOL": "pip",
                    "FRONTEND_BUILD_MODE": "fresh", "FAKE_CALLS": str(self.calls), "RUN_BACKEND_STAGES": "false"}
        for key in ("PYTHON_VENV_ACTIVATE", "TRUSTED_ANCHOR_ENVSETUP", "LOCAL_CI_TOOL_RESULT"):
            self.env.pop(key, None)

    def invoke(self, name, **changes):
        env = {**self.env, "LOCAL_CI_ARTIFACT_DIR": str(self.root / "artifacts" / name), **changes}
        completed = subprocess.run(["bash", str(ROOT / "tools/run_tool.sh"), name], env=env, text=True, capture_output=True)
        result_path = Path(env["LOCAL_CI_ARTIFACT_DIR"]) / "result.json"
        return completed, json.loads(result_path.read_text()) if result_path.exists() else None

    def test_frontend_build_does_not_install_or_invoke_other_stages(self):
        completed, result = self.invoke("frontend_build")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls, [["-m", "build", "--wheel", "--no-isolation"]])
        self.assertEqual(result["tool_id"], "frontend_build")
        state = json.loads((self.root / "task/state/frontend-wheel.json").read_text())
        self.assertEqual(state["tested_sha"], self.sha)
        self.assertEqual(state["sha256"], tool.digest(Path(state["path"])))

    def test_trusted_envsetup_preserves_task_venv_and_concurrency(self):
        setup = self.root / "trusted-envsetup.sh"
        setup.write_text('export MAX_JOBS=64 CMAKE_BUILD_PARALLEL_LEVEL=64 NINJAFLAGS=-j64\n'
                         'export PYTHON_BIN=/shared/python HOME=/shared/home ANCHOR_DIR=/shared/checkout\n'
                         'export LOCAL_CI_TASK_ROOT=/shared/task PATH=/usr/bin:/bin\n')
        with patch.dict(os.environ, {**self.env, "MAX_JOBS": "3", "CMAKE_BUILD_PARALLEL_LEVEL": "3", "NINJAFLAGS": "-j3"}, clear=True):
            ctx = tool.Context("frontend_build")
            ctx.source(setup)
        self.assertEqual(str(self.python), ctx.python)
        self.assertEqual(str(self.checkout), ctx.env["ANCHOR_DIR"])
        self.assertEqual(str(self.root / "task"), ctx.env["LOCAL_CI_TASK_ROOT"])
        self.assertEqual(("3", "3", "-j3"), tuple(ctx.env[key] for key in ("MAX_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL", "NINJAFLAGS")))
        self.assertTrue(ctx.env["PATH"].startswith(str(self.python.parent) + os.pathsep))

    def test_install_is_independent_and_validates_wheel_hash(self):
        self.assertEqual(self.invoke("frontend_build")[0].returncode, 0)
        completed, result = self.invoke("wheel_install_import")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("installation", result["details"])
        wheel = next((self.checkout / "dist").glob("*.whl"))
        wheel.write_bytes(b"tampered")
        previous = self.calls.read_text()
        completed, result = self.invoke("wheel_install_import")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("hash", result["error"])
        self.assertEqual(self.calls.read_text(), previous)

    def test_real_command_exit_code_is_preserved(self):
        self.assertEqual(self.invoke("frontend_build")[0].returncode, 0)
        completed, result = self.invoke("wheel_install_import", FAKE_INSTALL_EXIT="17")
        self.assertEqual(completed.returncode, 17)
        self.assertEqual(result["exit_code"], 17)
        self.assertEqual(result["commands"][-1]["exit_code"], 17)

    def backend_env(self):
        backend = self.root / "backend"
        backend.mkdir(exist_ok=True)
        return {"RUN_BACKEND_STAGES": "true", "BACKEND_PATH": str(backend), "BACKEND_ENVSETUP": "",
                "BACKEND_WHEEL_PATTERN": "triton_fixture_backend-*.whl", "EXPECTED_TRITON_BACKEND": "fixture",
                "BACKEND_TEST_COMMAND": shlex_join([sys.executable, "-c", "print('backend smoke and JIT fixture')"]),
                "FLAGGEMS_CLONE_DIR": str(self.root), "FLAGGEMS_RANDOM_SEED": "task-seed", "FLAGGEMS_AFFECTED_OPS": "abs"}

    def test_backend_build_and_jit_are_separate_commands(self):
        env = self.backend_env()
        completed, result = self.invoke("backend_rebuild", **env, FAKE_WHEEL_NAME="triton_fixture_backend-0.0-py3-none-any.whl")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        before = self.calls.read_text()
        calls = [json.loads(line) for line in before.splitlines()]
        self.assertEqual(sum(call[:2] == ["-m", "build"] for call in calls), 1)
        self.assertNotIn("tests/test_jit.py", before)
        completed, result = self.invoke("backend_smoke_jit", **env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["commands"][-1]["argv"][0], "bash")
        self.assertEqual(before, self.calls.read_text())

    def test_flaggems_and_performance_dispatch_only_selected_stage(self):
        env = self.backend_env()
        for name, variable in (("flaggems", ""), ("compile_time", "COMPILE_BENCHMARK_KERNELS"),
                               ("pass_profile", "PASS_PROFILE_KERNELS"), ("ir_serialization", "IR_SERIALIZATION_KERNELS")):
            with self.subTest(tool=name):
                self.calls.unlink(missing_ok=True)
                changes = {**env, **({variable: "add"} if variable else {})}
                completed, result = self.invoke(name, **changes)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                if name == "flaggems":
                    self.assertEqual(calls[0][calls[0].index("--seed") + 1], "task-seed")
                    self.assertEqual(calls[0][calls[0].index("--affected-ops") + 1], "abs")
                else:
                    self.assertEqual(result["details"]["performance"]["status"], "not_comparable")

    def test_wrong_sha_and_missing_backend_capability_fail(self):
        completed, result = self.invoke("frontend_build", LOCAL_CI_TESTED_SHA="0" * 40)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("SHA", result["error"])
        completed, result = self.invoke("backend_rebuild")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("capability", result["error"])
        self.assertFalse(self.calls.exists())

    def test_environment_fingerprint_survives_tool_receipt(self):
        llvm = self.root / "llvm"
        for relative in ("bin", "include/llvm", "include/mlir", "lib"):
            (llvm / relative).mkdir(parents=True, exist_ok=True)
        (llvm / "bin/llvm-config").write_text("fixture")
        hashfile = self.checkout / "triton/cmake/llvm-hash.txt"
        hashfile.parent.mkdir(parents=True)
        hashfile.write_text("a" * 40)
        ctx = tool.Context("environment", {**self.env, "LLVM_BUILD_DIR": str(llvm), "LOCAL_CI_LLVM_HASH": "a" * 40,
                                           "LOCAL_CI_MIN_FREE_BYTES": "0"})
        ctx.sha = self.sha
        def probe(args, **kwargs):
            if args[1:2] == ["-c"]:
                return json.dumps({"executable": str(self.python), "version": "fixture", "packages": {}})
            return "fixture-version\n"
        with patch.object(ctx, "run", side_effect=probe):
            tool.environment(ctx)
        self.assertTrue(ctx.fingerprint)
        ctx.finish(0)
        later = tool.Context("frontend_build", self.env)
        later.prepare()
        self.assertEqual(later.fingerprint, ctx.fingerprint)

    def test_symlinked_build_output_never_deletes_external_directory(self):
        external = self.root / "keep"
        external.mkdir()
        (external / "valuable").write_text("preserve")
        (self.checkout / "build").symlink_to(external, target_is_directory=True)
        completed, result = self.invoke("frontend_build")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual((external / "valuable").read_text(), "preserve")
        self.assertFalse(self.calls.exists())

    def test_timeout_terminates_command_and_is_not_success(self):
        ctx = tool.Context("contract_tests", {**self.env, "LOCAL_CI_TOOL_TIMEOUT_SECONDS": "1"})
        with self.assertRaises(tool.ToolError) as raised:
            ctx.run([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(raised.exception.code, 124)
        self.assertEqual(ctx.commands[-1]["exit_code"], 124)


class MeasurementTests(unittest.TestCase):
    def candidate(self, tool_id, median):
        measurement = {"count": 1, "median_ms": median}
        if tool_id == "compile_time":
            summary = {"all_correct": True, "compile_est": measurement}
        elif tool_id == "pass_profile":
            summary = {"passes": {"canonicalize": {"wall_ms": measurement}}}
        else:
            summary = {"module_count": 1, "metrics": {key: measurement for key in ("serialize", "deserialize", "roundtrip")}}
        return {"metadata": {"environment_fingerprint": "fixture-env", "commit_sha": "b" * 40},
                "summary": {"add": summary}, "events": [{"kernel": "add", "kind": "pass"}],
                "raw": [{"kernel": "add", "roundtrip_verified": True}]}

    def exercise_performance(self, tool_id, baseline, *, malformed=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            env = {**os.environ, "ANCHOR_DIR": str(checkout), "LOCAL_CI_TASK_ROOT": str(root / "task"),
                   "LOCAL_CI_ARTIFACT_DIR": str(root / "artifacts"), "PYTHON_BIN": sys.executable,
                   "EXPECTED_TRITON_BACKEND": "fixture", "FLAGGEMS_CLONE_DIR": directory,
                   "COMPILE_BENCHMARK_KERNELS": "add", "PASS_PROFILE_KERNELS": "add", "IR_SERIALIZATION_KERNELS": "add",
                   "LOCAL_CI_BASE_SHA": "b" * 40, "LOCAL_CI_ENVIRONMENT_FINGERPRINT": "fixture-env"}
            if baseline is not None:
                path = root / "baseline.json"
                path.write_text(json.dumps(baseline))
                env["BASELINE_JSON"] = str(path)
            else:
                env.pop("BASELINE_JSON", None)
            ctx = tool.Context(tool_id, env)
            ctx.backend, ctx.sha = checkout, "c" * 40
            candidate = self.candidate(tool_id, 200)
            if malformed:
                candidate["summary"] = {}
            original_run = ctx.run

            def boundary(args, **kwargs):
                if args[1].endswith("_benchmark.py"):
                    destination = Path(args[args.index("--output-json") + 1])
                    destination.write_text(json.dumps(candidate))
                    return ""
                return original_run(args, **kwargs)

            with patch.object(ctx, "run", side_effect=boundary):
                tool.performance(ctx)
            return ctx.details["performance"]

    def test_missing_or_incompatible_baselines_are_not_comparable(self):
        for name in ("compile_time", "pass_profile", "ir_serialization"):
            with self.subTest(tool=name):
                self.assertEqual(self.exercise_performance(name, None)["status"], "not_comparable")
                baseline = self.candidate(name, 100)
                baseline["metadata"]["environment_fingerprint"] = "other-env"
                self.assertEqual(self.exercise_performance(name, baseline)["reason"], "environment_fingerprint_mismatch")

    def test_slowdowns_use_real_comparators_and_do_not_fail_tools(self):
        for name in ("compile_time", "pass_profile", "ir_serialization"):
            with self.subTest(tool=name):
                result = self.exercise_performance(name, self.candidate(name, 100))
                self.assertEqual(result["status"], "warning")

    def test_malformed_candidates_fail_even_without_baseline(self):
        for name in ("compile_time", "pass_profile", "ir_serialization"):
            with self.subTest(tool=name), self.assertRaises(tool.ToolError):
                self.exercise_performance(name, None, malformed=True)

    def test_invalid_candidate_measurements_fail(self):
        for median in (None, float("nan"), float("inf"), -1, True):
            value = {"summary": {"add": {"all_correct": True, "compile_est": {"count": 1, "median_ms": median}}}}
            with self.assertRaises(tool.ToolError):
                tool.validate_measurements("compile_time", value, ["add"])
        with self.assertRaises(tool.ToolError):
            tool.validate_measurements("pass_profile", {"summary": {"add": {"passes": {}}}, "events": []}, ["add"])

    def test_profile_compare_rejects_missing_candidate_passes(self):
        with self.assertRaises(ValueError):
            profile_compare.compare(None, {"summary": {"add": {"passes": {}}}}, ["add"], .2, 1, 1, 10, "slowdown", "base", "head")

    def test_compile_benchmark_rejects_empty_or_zero_sample(self):
        for kernels, repeat in (("", 1), ("add", 0)):
            with self.assertRaises(ValueError):
                compile_benchmark.run_parent(argparse.Namespace(kernels=kernels, repeat=repeat, warmup=0))

    def test_pass_profile_no_events_is_an_execution_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            args = argparse.Namespace(kernels="add", repeat=1, warmup=0, top_n=10,
                                      flaggems_root=directory, cache_root=str(root / "benchmark/cache"), backend="fixture",
                                      keep_workdirs=False)
            with patch.object(profile_benchmark, "run_child", return_value=({"compile_est_ms": 1}, [])):
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
            ir = types.SimpleNamespace(parse_mlir_module=lambda p, c: Module(Path(p).read_text()))
            binding = types.ModuleType("triton._C.libtriton")
            binding.ir = ir
            with patch.dict(sys.modules, {"triton._C.libtriton": binding}):
                row = ir_benchmark.measure_once([Module("module {}\n")], None, [path], "add", "repeat", 0)
                self.assertTrue(row["roundtrip_verified"])
                ir.parse_mlir_module = lambda p, c: Module("module { changed }\n")
                with self.assertRaisesRegex(RuntimeError, "canonical"):
                    ir_benchmark.measure_once([Module("module {}\n")], None, [path], "add", "repeat", 0)
                ir.parse_mlir_module = lambda p, c: Module("module {}\n", False)
                with self.assertRaisesRegex(RuntimeError, "invalid MLIR"):
                    ir_benchmark.measure_once([Module("module {}\n")], None, [path], "add", "repeat", 0)


class FlagGemsSelectionTests(unittest.TestCase):
    def test_task_cache_cleanup_cannot_touch_shared_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, dump = root / "cache", root / "dump"
            with patch.dict(os.environ, {"LOCAL_CI_TASK_ROOT": directory, "TRITON_CACHE_DIR": str(cache)}):
                self.assertEqual(batch.task_cache_paths(dump), [cache, dump])
                with self.assertRaises(ValueError):
                    batch.task_cache_paths(root.parent / "shared-cache")

    def test_sample_reproducibility_and_affected_operator_union(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_ops.py").write_text("\n".join("@pytest.mark." + op for op in ("add", "abs", "mm", "softmax")))
            whitelist = root / "pass.tsv"
            whitelist.write_text("pointwise add add\npointwise abs abs\nmatrix mm mm\n")
            full = root / "full.tsv"
            full.write_text(whitelist.read_text() + "reduction softmax softmax\n")
            args = argparse.Namespace(mode="sample", sample_size=2, seed="trusted-task-seed", op="",
                                      affected_ops="softmax", whitelist=str(whitelist), full_list=str(full), flaggems_dir=directory)
            first = selector.select_entries(args)
            self.assertEqual(first, selector.select_entries(args))
            self.assertEqual({e.category for e in first}, {"pointwise", "matrix", "reduction"})
            self.assertIn("softmax", [e.op for e in first])
            args.affected_ops = "does_not_exist"
            with self.assertRaisesRegex(ValueError, "trusted test mapping"):
                selector.select_entries(args)


if __name__ == "__main__":
    unittest.main()
