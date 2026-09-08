"""Tool boundary tests; optional real pure-Python wheel roundtrip integration.

Set CI_TOOLS_TEST_PYTHON to an isolated interpreter with build/setuptools/wheel
to exercise actual build/install/import/smoke commands. This does not simulate or
claim a Triton compiler/backend build.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.basic_tools import actions, runner  # noqa: E402

FG_ROOT = ROOT / "tools" / "basic_tools" / "flaggems"
sys.path.insert(0, str(FG_ROOT))
from select_flaggems_tests import select_entries  # noqa: E402


def context() -> dict:
    return {"source_dir": "/workspace/tasks/test/source", "artifact_dir": "/workspace/tasks/test/artifacts",
            "task_id": "test", "target_sha": "a" * 40, "base_sha": "b" * 40, "triton_version": "3.0",
            "tools_dir": "/opt/anchor-ci/tools", "completed_tools": list(runner.TOOL_IDS),
            "profile": {"id": "triton-3.0", "llvm_revision": "c" * 40, "tools": {
                "backend_dir": "/workspace/backend", "backend_wheel_pattern": "triton_sophgo-*.whl",
                "expected_backend": "sophgo", "backend_smoke_argv": ["python3", "tests/jit.py"],
                "flaggems_dir": "/workspace/FlagGems"}}}


class ToolPlanningTests(unittest.TestCase):
    def test_all_tools_plan_without_local_container_paths(self):
        for name in runner.TOOL_IDS:
            with self.subTest(name=name):
                result = runner.plan(name, context())
                self.assertEqual(result["status"], "ready")
                self.assertTrue(result["commands"])
                self.assertTrue(all(cmd["argv"] and cmd["cwd"] and cmd["timeout"] > 0 for cmd in result["commands"]))

    def test_missing_backend_on_three_zero_is_configuration_error(self):
        ctx = context()
        ctx["profile"]["tools"].pop("backend_smoke_argv")
        with self.assertRaisesRegex(ValueError, "real backend JIT"):
            runner.plan("backend_smoke", ctx)

    def test_backend_tools_not_applicable_on_other_versions(self):
        ctx = context()
        ctx["triton_version"] = "3.2"
        for name in runner.BACKEND_TOOLS:
            result = runner.plan(name, ctx)
            self.assertEqual(result["status"], "not_applicable")
            self.assertEqual(result["commands"], [])

    def test_full_operator_selection_requires_trusted_manual_task(self):
        with self.assertRaisesRegex(ValueError, "manual_full"):
            runner.plan("flaggems", context(), {"mode": "full"})
        ctx = context()
        ctx["manual_full"] = True
        self.assertEqual(runner.plan("flaggems", ctx, {"mode": "full"})["status"], "ready")

    def test_agent_cannot_override_commands_profile_or_dependencies(self):
        for params in ({"argv": ["true"]}, {"profile": {}}, {"completed_tools": ["frontend_build"]}):
            with self.assertRaises(ValueError):
                runner.plan("wheel_install", context(), params)
        ctx = context()
        ctx["completed_tools"] = []
        with self.assertRaisesRegex(ValueError, "successful tool receipts"):
            runner.plan("wheel_install", ctx)

    def test_build_recovery_parallelism_is_bounded(self):
        for jobs in (0, 65, True, "1"):
            with self.assertRaises(ValueError):
                runner.plan("frontend_build", context(), {"jobs": jobs})
        spec = runner.plan("frontend_build", context(), {"jobs": 1})
        self.assertTrue(all(cmd["env"]["CMAKE_BUILD_PARALLEL_LEVEL"] == "1" for cmd in spec["commands"]))

    def test_environment_script_arguments_are_data(self):
        ctx = context()
        ctx["profile"]["tools"]["env_scripts"] = [{"path": "/opt/sdk/env.sh", "args": ["x; touch /tmp/injected"]}]
        spec = runner.plan("environment", ctx)
        argv = spec["commands"][0]["argv"]
        self.assertIn("x; touch /tmp/injected", argv)
        self.assertEqual(argv[:3], ["bash", "/opt/anchor-ci/tools/basic_tools/env_exec.sh", "/opt/sdk/env.sh"])

    def test_backend_jit_uses_the_same_task_python_as_wheel_installation(self):
        ctx = context()
        ctx["python_bin"] = "/workspace/tasks/test/venv/bin/python3"
        spec = runner.plan("backend_smoke", ctx)
        self.assertEqual(spec["commands"][-2]["argv"][0], ctx["python_bin"])
        self.assertEqual(spec["commands"][-2]["env"]["PYTHON_BIN"], ctx["python_bin"])

    def test_trusted_actions_skip_task_startup_and_pass_candidate_wrapper_explicitly(self):
        ctx = context()
        ctx.update(python_bin="/opt/anchor-ci/runtime/task_python", task_venv="/workspace/tasks/test/run/venv")
        spec = runner.plan("frontend_build", ctx)
        helper = spec["commands"][0]
        self.assertEqual(helper["argv"][:3], ["/usr/bin/python3", "-I", "-S"])
        payload = json.loads(helper["argv"][-1])
        self.assertEqual(payload["context"]["python_bin"], ctx["python_bin"])
        self.assertEqual(helper["env"]["LOCAL_CI_TASK_VENV"], ctx["task_venv"])
        self.assertEqual(spec["commands"][2]["argv"][0], ctx["python_bin"])

    def test_example_profile_can_plan_real_flaggems_invocation(self):
        configuration = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        ctx = context()
        ctx["profile"] = next(p for p in configuration["profiles"] if p["triton_version"] == "3.0")
        spec = runner.plan("flaggems", ctx, {"mode": "impact", "ops": ["add"]})
        self.assertIn("--pytest-args=--ref cpu -vs", spec["commands"][-1]["argv"])


class ArtifactAndSelectionTests(unittest.TestCase):
    def test_contaminated_task_startup_cannot_skip_trusted_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            venv.EnvBuilder(with_pip=False).create(root / "venv")
            executable = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            site = Path(subprocess.check_output([str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
            site.mkdir(parents=True, exist_ok=True)
            (site / "sitecustomize.py").write_text("import os\nos._exit(0)\n")
            (root / "source").mkdir()
            ctx = context()
            ctx.update(source_dir=str(root / "source"), artifact_dir=str(root / "artifacts"),
                       python_bin=str(executable), trusted_python_bin=sys.executable, tools_dir=str(ROOT / "tools"))
            result = runner.execute("frontend_build", ctx)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(len(result["execution"]), 1)
            self.assertNotEqual(result["execution"][0]["returncode"], 0)

    def test_wheel_hash_and_task_identity_are_checked_before_install(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            ctx["artifact_dir"] = folder
            out = Path(folder) / "frontend_build"
            (out / "wheels").mkdir(parents=True)
            wheel = out / "wheels" / "triton_anchor-test.whl"
            wheel.write_bytes(b"original-wheel")
            manifest = {"task_id": ctx["task_id"], "target_sha": ctx["target_sha"],
                        "wheel": str(wheel), "sha256": actions.digest(wheel)}
            actions.write_json(out / "wheel.json", manifest)
            self.assertEqual(actions.wheel_manifest(ctx, "frontend_build")[1], wheel)
            wheel.write_bytes(b"modified-wheel")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                actions.wheel_manifest(ctx, "frontend_build")
            manifest["task_id"] = "older-task"
            actions.write_json(out / "wheel.json", manifest)
            with self.assertRaisesRegex(ValueError, "different tested commit/task"):
                actions.wheel_manifest(ctx, "frontend_build")

    def test_build_cleanup_refuses_outside_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            root.mkdir()
            sentinel = Path(folder) / "keep.txt"
            sentinel.write_text("keep")
            with self.assertRaises(ValueError):
                actions.remove_child(root, sentinel)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_rebuilt_wheel_invalidates_previous_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            ctx["artifact_dir"] = folder
            out = Path(folder) / "frontend_build"
            (out / "wheels").mkdir(parents=True)
            wheel = out / "wheels" / "triton_anchor-test.whl"
            wheel.write_bytes(b"old")
            manifest = {"task_id": ctx["task_id"], "target_sha": ctx["target_sha"],
                        "wheel": str(wheel), "sha256": actions.digest(wheel)}
            actions.write_json(out / "wheel.json", manifest)
            actions.write_json(Path(folder) / "wheel_install" / "installation.json",
                               {**manifest, "python_executable": sys.executable})
            actions.require_installation(ctx, "frontend_build", "wheel_install")
            wheel.write_bytes(b"new")
            actions.write_json(out / "wheel.json", {**manifest, "sha256": actions.digest(wheel)})
            with self.assertRaisesRegex(ValueError, "reinstall"):
                actions.require_installation(ctx, "frontend_build", "wheel_install")

    def test_impact_includes_explicit_operator_outside_pass_whitelist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            (root / "tests" / "test_ops.py").write_text("@pytest.mark.abs\ndef test_abs(): pass\n@pytest.mark.gelu\ndef test_gelu(): pass\n")
            (root / "pass.tsv").write_text("unary abs abs\n")
            (root / "all.tsv").write_text("unary abs abs\nunary gelu gelu\n")
            args = argparse.Namespace(mode="impact", ops="gelu", categories="", flaggems_dir=str(root),
                                      whitelist=str(root / "pass.tsv"), full_list=str(root / "all.tsv"))
            self.assertEqual([entry.op for entry in select_entries(args)], ["gelu"])
            args.ops = "not_an_operator"
            with self.assertRaisesRegex(ValueError, "Unknown FlagGems"):
                select_entries(args)
            args.ops = ""
            self.assertEqual([entry.op for entry in select_entries(args)], ["abs"])

    def test_performance_missing_baseline_is_explicit_and_regression_is_nonblocking(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            ctx["artifact_dir"] = folder
            out = Path(folder) / "compile_time"
            actions.write_json(out / "candidate.json", {"summary": {"add": {"compile_est": {"median_ms": 20}}}})
            payload = {"tool_id": "compile_time", "context": ctx, "parameters": {}, "kernels": ["add"]}
            actions.compare_performance(payload)
            result = actions.read_json(out / "comparison.json")
            self.assertFalse(result["baseline_available"])
            self.assertIsNone(result["kernels"][0]["change_ratio"])
            baseline = Path(folder) / "baseline.json"
            actions.write_json(baseline, {"summary": {"add": {"compile_est": {"median_ms": 10}}}})
            ctx["performance_baselines"] = {"compile_time": {"path": str(baseline), "sha256": actions.digest(baseline),
                "base_sha": ctx["base_sha"], "profile_id": ctx["profile"]["id"], "llvm_revision": ctx["profile"]["llvm_revision"]}}
            actions.compare_performance(payload)
            result = actions.read_json(out / "comparison.json")
            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["kernels"][0]["change_ratio"], 1.0)
            baseline.write_text("{}")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                actions.compare_performance(payload)


@unittest.skipUnless(os.environ.get("CI_TOOLS_TEST_PYTHON"), "Set CI_TOOLS_TEST_PYTHON for real isolated wheel integration")
class RealWheelIntegrationTests(unittest.TestCase):
    def test_real_build_install_import_smoke_then_rebuild(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "triton_anchor").mkdir()
            (source / "triton_anchor" / "__init__.py").write_text("__version__ = '0.0.1'\nVALUE = 42\n")
            (source / "tests").mkdir()
            (source / "tests" / "test_smoke.py").write_text("import triton_anchor\nassert triton_anchor.VALUE == 42\nprint('pure-Python fixture smoke passed')\n")
            (source / "setup.py").write_text("from setuptools import setup\nsetup(name='triton-anchor', version='0.0.1', packages=['triton_anchor'])\n")
            (source / "pyproject.toml").write_text('[build-system]\nrequires=["setuptools", "wheel"]\nbuild-backend="setuptools.build_meta"\n')
            for command in (["git", "init", "--quiet"], ["git", "add", "."],
                            ["git", "-c", "user.name=CI fixture", "-c", "user.email=fixture@invalid", "commit", "--quiet", "-m", "fixture"]):
                subprocess.run(command, cwd=source, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            ctx = context()
            ctx.update(source_dir=str(source), artifact_dir=str(root / "artifacts"), target_sha=sha,
                       triton_version="3.2", python_bin=os.environ["CI_TOOLS_TEST_PYTHON"], trusted_python_bin=sys.executable,
                       tools_dir=str(ROOT / "tools"), completed_tools=[])
            ctx["profile"]["tools"] = {"required_commands": ["git"], "required_modules": ["build", "setuptools", "wheel"]}
            for tool in ("environment", "frontend_build", "wheel_install", "frontend_smoke"):
                result = runner.execute(tool, ctx)
                self.assertEqual(result["status"], "pass", tool)
                self.assertFalse(result["trusted_receipt"])
                ctx["completed_tools"].append(tool)
            old_manifest, old_wheel = actions.wheel_manifest(ctx, "frontend_build")
            self.assertTrue(old_wheel.is_file())
            # A rebuild must clear stale wheel output so there is exactly one artifact.
            (old_wheel.parent / "triton_anchor-stale.whl").write_bytes(b"old")
            self.assertEqual(runner.execute("frontend_build", ctx, {"jobs": 1})["status"], "pass")
            self.assertEqual(len(list(old_wheel.parent.glob("*.whl"))), 1)
            self.assertEqual(actions.wheel_manifest(ctx, "frontend_build")[0]["target_sha"], sha)
            ctx["target_sha"] = "0" * 40
            self.assertEqual(runner.execute("environment", ctx)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
