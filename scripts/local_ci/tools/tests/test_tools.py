"""Essential behavior checks for related CI responsibilities."""

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

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))

from tools.basic_tools import actions, runner  # noqa: E402


FG_ROOT = ROOT / "tools" / "basic_tools" / "flaggems"

sys.path.insert(0, str(FG_ROOT))

from select_flaggems_tests import select_entries  # noqa: E402


def context() -> dict:
    return {
        "source_dir": "/workspace/tasks/test/source",
        "artifact_dir": "/workspace/tasks/test/artifacts",
        "task_id": "test",
        "target_sha": "a" * 40,
        "base_sha": "b" * 40,
        "triton_version": "3.0",
        "tools_dir": "/opt/anchor-ci/tools",
        "completed_tools": list(runner.TOOL_IDS),
        "profile": {
            "id": "triton-3.0",
            "llvm_revision": "c" * 40,
            "tools": {
                "backend_dir": "/workspace/backend",
                "backend_wheel_pattern": "triton_sophgo-*.whl",
                "expected_backend": "sophgo",
                "backend_smoke_argv": ["python3", "tests/jit.py"],
                "backend_test_paths": ["tests"],
                "flaggems_dir": "/workspace/FlagGems",
            },
        },
    }


class ToolPlanningTests(unittest.TestCase):
    def test_all_tools_plan_without_local_container_paths(self):
        for name in runner.TOOL_IDS:
            with self.subTest(name=name):
                result = runner.plan(name, context())
                self.assertEqual(result["status"], "ready")
                self.assertTrue(result["commands"])
                self.assertTrue(
                    all(
                        cmd["argv"] and cmd["cwd"] and cmd["timeout"] > 0
                        for cmd in result["commands"]
                    )
                )

    def test_missing_backend_on_three_zero_is_configuration_error(self):
        ctx = context()
        ctx["profile"]["tools"].pop("backend_smoke_argv")
        with self.assertRaisesRegex(ValueError, "real backend JIT"):
            runner.plan("backend_smoke", ctx)
        ctx = context()
        ctx["profile"]["tools"].pop("backend_test_paths")
        with self.assertRaisesRegex(ValueError, "backend_test_paths"):
            runner.plan("backend_tests", ctx)

    def test_builds_and_suites_have_independent_dependencies(self):
        ctx = context()
        ctx["completed_tools"] = ["environment"]
        for tool in ("frontend_build", "backend_build"):
            spec = runner.plan(tool, ctx)
            self.assertEqual(spec["dependencies"], ["environment"])
            actions_used = [
                command["argv"][-2]
                for command in spec["commands"]
                if "actions.py" in " ".join(command["argv"])
            ]
            self.assertNotIn("install_wheel", actions_used)
            self.assertNotIn("backend_discovery", actions_used)
        ctx["completed_tools"] = ["backend_build"]
        with self.assertRaisesRegex(ValueError, "frontend_install"):
            runner.plan("backend_install", ctx)
        ctx["completed_tools"].append("frontend_install")
        self.assertEqual(
            runner.plan("backend_install", ctx)["dependencies"],
            ["backend_build", "frontend_install"],
        )
        ctx["completed_tools"] = ["frontend_install", "backend_install"]
        for tool in (
            "frontend_tests",
            "backend_tests",
            "frontend_smoke",
            "backend_smoke",
        ):
            self.assertEqual(runner.plan(tool, ctx)["status"], "ready")

    def test_missing_backend_sdk_does_not_wrap_frontend_commands(self):
        ctx = context()
        ctx["profile"]["tools"]["backend_env_scripts"] = [
            {"path": "/unavailable/backend/envsetup.sh", "args": ["PIO_CMODEL"]}
        ]
        for tool in (
            "environment",
            "frontend_build",
            "frontend_install",
            "frontend_tests",
            "frontend_smoke",
        ):
            self.assertTrue(
                all(
                    command["argv"][0] != "bash"
                    for command in runner.plan(tool, ctx)["commands"]
                )
            )
        for tool in runner.BACKEND_TOOLS:
            self.assertTrue(
                all(
                    command["argv"][:2]
                    == ["bash", "/opt/anchor-ci/tools/basic_tools/env_exec.sh"]
                    for command in runner.plan(tool, ctx)["commands"]
                )
            )

    def test_selected_suite_nodes_stay_within_trusted_test_roots(self):
        ctx = context()
        ctx["python_bin"] = "/opt/anchor-ci/runtime/task_python"
        selected = "tests/test_math.py::test_add"
        spec = runner.plan(
            "backend_tests", ctx, {"paths": [selected], "keyword": "add and not slow"}
        )
        command = spec["commands"][-2]
        self.assertEqual(
            command["argv"][:3],
            [
                ctx["python_bin"],
                "-I",
                "/opt/anchor-ci/tools/basic_tools/pytest_exec.py",
            ],
        )
        self.assertIn("/workspace/backend/" + selected, command["argv"])
        self.assertIn("add and not slow", command["argv"])
        self.assertEqual(
            command["cwd"], "/workspace/tasks/test/artifacts/backend_tests"
        )
        for paths in (
            [],
            ["../outside.py"],
            ["/tmp/test.py"],
            ["tests/../../escape.py"],
            ["setup.py"],
            ["C:/outside.py"],
        ):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                runner.plan("backend_tests", ctx, {"paths": paths})

    def test_backend_tools_not_applicable_on_other_versions(self):
        ctx = context()
        ctx["triton_version"] = "3.2"
        for name in runner.BACKEND_TOOLS:
            result = runner.plan(name, ctx)
            self.assertEqual(result["status"], "not_applicable")
            self.assertEqual(result["commands"], [])

    def test_full_operator_selection_is_available_to_policy(self):
        self.assertEqual(
            runner.plan("flaggems", context(), {"mode": "full"})["status"], "ready"
        )
        ctx = context()
        ctx["manual_full"] = True
        self.assertEqual(
            runner.plan("flaggems", ctx, {"mode": "full"})["status"], "ready"
        )

    def test_agent_cannot_override_commands_profile_or_dependencies(self):
        for params in (
            {"argv": ["true"]},
            {"profile": {}},
            {"completed_tools": ["frontend_build"]},
        ):
            with self.assertRaises(ValueError):
                runner.plan("frontend_install", context(), params)
        ctx = context()
        ctx["completed_tools"] = []
        with self.assertRaisesRegex(ValueError, "successful tool receipts"):
            runner.plan("frontend_install", ctx)

    def test_build_recovery_parallelism_is_bounded(self):
        for jobs in (0, 65, True, "1"):
            with self.assertRaises(ValueError):
                runner.plan("frontend_build", context(), {"jobs": jobs})
        spec = runner.plan("frontend_build", context(), {"jobs": 1})
        self.assertTrue(
            all(
                cmd["env"]["CMAKE_BUILD_PARALLEL_LEVEL"] == "1"
                for cmd in spec["commands"]
            )
        )

    def test_environment_script_arguments_are_data(self):
        ctx = context()
        ctx["profile"]["tools"]["env_scripts"] = [
            {"path": "/opt/sdk/env.sh", "args": ["x; touch /tmp/injected"]}
        ]
        spec = runner.plan("environment", ctx)
        argv = spec["commands"][0]["argv"]
        self.assertIn("x; touch /tmp/injected", argv)
        self.assertEqual(
            argv[:3],
            ["bash", "/opt/anchor-ci/tools/basic_tools/env_exec.sh", "/opt/sdk/env.sh"],
        )

    def test_backend_jit_uses_the_same_task_python_as_frontend_installation(self):
        ctx = context()
        ctx["python_bin"] = "/workspace/tasks/test/venv/bin/python3"
        spec = runner.plan("backend_smoke", ctx)
        self.assertEqual(spec["commands"][-2]["argv"][0], ctx["python_bin"])
        self.assertEqual(spec["commands"][-2]["env"]["PYTHON_BIN"], ctx["python_bin"])

    def test_trusted_actions_skip_task_startup_and_pass_candidate_wrapper_explicitly(
        self,
    ):
        ctx = context()
        ctx.update(
            python_bin="/opt/anchor-ci/runtime/task_python",
            task_venv="/workspace/tasks/test/run/venv",
        )
        spec = runner.plan("frontend_build", ctx)
        helper = spec["commands"][0]
        self.assertEqual(helper["argv"][:3], ["/usr/bin/python3", "-I", "-S"])
        payload = json.loads(helper["argv"][-1])
        self.assertEqual(payload["context"]["python_bin"], ctx["python_bin"])
        self.assertEqual(helper["env"]["LOCAL_CI_TASK_VENV"], ctx["task_venv"])
        self.assertEqual(spec["commands"][2]["argv"][0], ctx["python_bin"])


class ArtifactAndSelectionTests(unittest.TestCase):
    def test_junit_requires_real_passing_cases(self):
        reports = {
            "<testsuites><testsuite tests='5'/></testsuites>": False,
            "<testsuite><testcase><skipped/></testcase></testsuite>": False,
            "<testsuite><testcase/><testcase><failure/></testcase></testsuite>": False,
            "<testsuite><testcase/><testcase><skipped/></testcase></testsuite>": True,
        }
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            ctx["artifact_dir"] = folder
            out = Path(folder) / "frontend_tests"
            out.mkdir()
            payload = {
                "context": ctx,
                "tool_id": "frontend_tests",
                "parameters": {},
                "test_source": ctx["source_dir"],
                "test_paths": ["tests"],
            }
            for xml, accepted in reports.items():
                with self.subTest(xml=xml):
                    (out / "tests.xml").write_text(xml)
                    if accepted:
                        actions.test_results(payload)
                        summary = actions.read_json(out / "tests.json")
                        self.assertEqual(summary["passed"], 1)
                        self.assertEqual(
                            summary["junit_sha256"], actions.digest(out / "tests.xml")
                        )
                    else:
                        with self.assertRaisesRegex(ValueError, "passing cases"):
                            actions.test_results(payload)

    def test_contaminated_task_startup_cannot_skip_trusted_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            venv.EnvBuilder(with_pip=False).create(root / "venv")
            executable = (
                root
                / "venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            site = Path(
                subprocess.check_output(
                    [
                        str(executable),
                        "-c",
                        "import sysconfig; print(sysconfig.get_path('purelib'))",
                    ],
                    text=True,
                ).strip()
            )
            site.mkdir(parents=True, exist_ok=True)
            (site / "sitecustomize.py").write_text("import os\nos._exit(0)\n")
            (root / "source").mkdir()
            ctx = context()
            ctx.update(
                source_dir=str(root / "source"),
                artifact_dir=str(root / "artifacts"),
                python_bin=str(executable),
                trusted_python_bin=sys.executable,
                tools_dir=str(ROOT / "tools"),
            )
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
            manifest = {
                "task_id": ctx["task_id"],
                "target_sha": ctx["target_sha"],
                "wheel": str(wheel),
                "sha256": actions.digest(wheel),
            }
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
            manifest = {
                "task_id": ctx["task_id"],
                "target_sha": ctx["target_sha"],
                "wheel": str(wheel),
                "sha256": actions.digest(wheel),
            }
            actions.write_json(out / "wheel.json", manifest)
            actions.write_json(
                Path(folder) / "frontend_install" / "installation.json",
                {**manifest, "python_executable": sys.executable},
            )
            actions.require_installation(ctx, "frontend_build", "frontend_install")
            wheel.write_bytes(b"new")
            actions.write_json(
                out / "wheel.json", {**manifest, "sha256": actions.digest(wheel)}
            )
            with self.assertRaisesRegex(ValueError, "reinstall"):
                actions.require_installation(ctx, "frontend_build", "frontend_install")

    def test_impact_includes_explicit_operator_outside_pass_whitelist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tests").mkdir()
            (root / "tests" / "test_ops.py").write_text(
                "@pytest.mark.abs\ndef test_abs(): pass\n@pytest.mark.gelu\ndef test_gelu(): pass\n"
            )
            (root / "pass.tsv").write_text("unary abs abs\n")
            (root / "all.tsv").write_text("unary abs abs\nunary gelu gelu\n")
            args = argparse.Namespace(
                mode="impact",
                ops="gelu",
                categories="",
                flaggems_dir=str(root),
                whitelist=str(root / "pass.tsv"),
                full_list=str(root / "all.tsv"),
            )
            self.assertEqual([entry.op for entry in select_entries(args)], ["gelu"])
            args.ops = "not_an_operator"
            with self.assertRaisesRegex(ValueError, "Unknown FlagGems"):
                select_entries(args)
            args.ops = ""
            self.assertEqual([entry.op for entry in select_entries(args)], ["abs"])
