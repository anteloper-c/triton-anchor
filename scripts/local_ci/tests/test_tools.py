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

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from tools.basic_tools import actions, runner  # noqa: E402

from runtime.policy import minimum_checks  # noqa: E402

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
                "backend_test_paths": ["tests"],
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
            actions_used = [command["argv"][-2] for command in spec["commands"] if "actions.py" in " ".join(command["argv"])]
            self.assertNotIn("install_wheel", actions_used)
            self.assertNotIn("backend_discovery", actions_used)
        ctx["completed_tools"] = ["backend_build"]
        with self.assertRaisesRegex(ValueError, "frontend_install"):
            runner.plan("backend_install", ctx)
        ctx["completed_tools"].append("frontend_install")
        self.assertEqual(runner.plan("backend_install", ctx)["dependencies"], ["backend_build", "frontend_install"])
        ctx["completed_tools"] = ["frontend_install", "backend_install"]
        for tool in ("frontend_tests", "backend_tests", "frontend_smoke", "backend_smoke"):
            self.assertEqual(runner.plan(tool, ctx)["status"], "ready")

    def test_missing_backend_sdk_does_not_wrap_frontend_commands(self):
        ctx = context()
        ctx["profile"]["tools"]["backend_env_scripts"] = [
            {"path": "/unavailable/backend/envsetup.sh", "args": ["PIO_CMODEL"]}]
        for tool in ("environment", "frontend_build", "frontend_install", "frontend_tests", "frontend_smoke"):
            self.assertTrue(all(command["argv"][0] != "bash" for command in runner.plan(tool, ctx)["commands"]))
        for tool in runner.BACKEND_TOOLS:
            self.assertTrue(all(command["argv"][:2] == ["bash", "/opt/anchor-ci/tools/basic_tools/env_exec.sh"]
                                for command in runner.plan(tool, ctx)["commands"]))

    def test_selected_suite_nodes_stay_within_trusted_test_roots(self):
        ctx = context()
        ctx["python_bin"] = "/opt/anchor-ci/runtime/task_python"
        selected = "tests/test_math.py::test_add"
        spec = runner.plan("backend_tests", ctx, {"paths": [selected], "keyword": "add and not slow"})
        command = spec["commands"][-2]
        self.assertEqual(command["argv"][:4], [ctx["python_bin"], "-I", "-m", "pytest"])
        self.assertIn("/workspace/backend/" + selected, command["argv"])
        self.assertIn("add and not slow", command["argv"])
        self.assertEqual(command["cwd"], "/workspace/tasks/test/artifacts/backend_tests")
        for paths in ([], ["../outside.py"], ["/tmp/test.py"], ["tests/../../escape.py"], ["setup.py"], ["C:/outside.py"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                runner.plan("backend_tests", ctx, {"paths": paths})

    def test_policy_keeps_minimum_and_expands_applicable_suites(self):
        for version in ("3.0", "3.0.1", "3.3"):
            compiler = minimum_checks(["python/triton_anchor/anchor_ir.py"], {"triton_version": version})
            self.assertTrue(set(runner.MINIMUM_FRONTEND) <= set(compiler["required"]))
            self.assertIn("frontend_tests", compiler["required"])
            unknown = minimum_checks(["unclassified.data"], {"triton_version": version})
            self.assertTrue(set(unknown["supported"]) <= set(unknown["required"]))
            if version == "3.3":
                self.assertFalse(set(runner.BACKEND_TOOLS) & set(unknown["required"]))
            else:
                self.assertIn("backend_tests", unknown["required"])
        docs = minimum_checks(["docs/guide.md"], {"triton_version": "3.0"})
        self.assertEqual(docs["required"], ["architecture_review"])

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
        self.assertTrue(all(cmd["env"]["CMAKE_BUILD_PARALLEL_LEVEL"] == "1" for cmd in spec["commands"]))

    def test_environment_script_arguments_are_data(self):
        ctx = context()
        ctx["profile"]["tools"]["env_scripts"] = [{"path": "/opt/sdk/env.sh", "args": ["x; touch /tmp/injected"]}]
        spec = runner.plan("environment", ctx)
        argv = spec["commands"][0]["argv"]
        self.assertIn("x; touch /tmp/injected", argv)
        self.assertEqual(argv[:3], ["bash", "/opt/anchor-ci/tools/basic_tools/env_exec.sh", "/opt/sdk/env.sh"])

    def test_backend_jit_uses_the_same_task_python_as_frontend_installation(self):
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

class ArtifactAndSelectionTests(unittest.TestCase):
    def test_junit_requires_real_passing_cases(self):
        reports = {"<testsuites><testsuite tests='5'/></testsuites>": False,
                   "<testsuite><testcase><skipped/></testcase></testsuite>": False,
                   "<testsuite><testcase/><testcase><failure/></testcase></testsuite>": False,
                   "<testsuite><testcase/><testcase><skipped/></testcase></testsuite>": True}
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            ctx["artifact_dir"] = folder
            out = Path(folder) / "frontend_tests"
            out.mkdir()
            payload = {"context": ctx, "tool_id": "frontend_tests", "parameters": {},
                       "test_source": ctx["source_dir"], "test_paths": ["tests"]}
            for xml, accepted in reports.items():
                with self.subTest(xml=xml):
                    (out / "tests.xml").write_text(xml)
                    if accepted:
                        actions.test_results(payload)
                        summary = actions.read_json(out / "tests.json")
                        self.assertEqual(summary["passed"], 1)
                        self.assertEqual(summary["junit_sha256"], actions.digest(out / "tests.xml"))
                    else:
                        with self.assertRaisesRegex(ValueError, "passing cases"):
                            actions.test_results(payload)

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
            actions.write_json(Path(folder) / "frontend_install" / "installation.json",
                               {**manifest, "python_executable": sys.executable})
            actions.require_installation(ctx, "frontend_build", "frontend_install")
            wheel.write_bytes(b"new")
            actions.write_json(out / "wheel.json", {**manifest, "sha256": actions.digest(wheel)})
            with self.assertRaisesRegex(ValueError, "reinstall"):
                actions.require_installation(ctx, "frontend_build", "frontend_install")

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
            (source / "tests" / "test_unit.py").write_text("import triton_anchor\ndef test_installed_value():\n    assert triton_anchor.VALUE == 42\n")
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
            ctx["profile"]["tools"] = {"required_commands": ["git"], "required_modules": ["build", "setuptools", "wheel", "pytest"],
                                         "frontend_test_paths": ["tests"]}
            for tool in ("environment", "frontend_build", "frontend_install", "frontend_tests", "frontend_smoke"):
                result = runner.execute(tool, ctx)
                self.assertEqual(result["status"], "pass", tool)
                self.assertFalse(result["trusted_receipt"])
                ctx["completed_tools"].append(tool)
            self.assertEqual(actions.read_json(root / "artifacts/frontend_tests/tests.json")["passed"], 1)
            self.assertEqual(runner.execute("frontend_tests", ctx, {"keyword": "missing_test_name"})["status"], "fail")
            old_manifest, old_wheel = actions.wheel_manifest(ctx, "frontend_build")
            self.assertTrue(old_wheel.is_file())
            # A rebuild must clear stale wheel output so there is exactly one artifact.
            (old_wheel.parent / "triton_anchor-stale.whl").write_bytes(b"old")
            self.assertEqual(runner.execute("frontend_build", ctx, {"jobs": 1})["status"], "pass")
            self.assertEqual(len(list(old_wheel.parent.glob("*.whl"))), 1)
            self.assertEqual(actions.wheel_manifest(ctx, "frontend_build")[0]["target_sha"], sha)
            ctx["target_sha"] = "0" * 40
            self.assertEqual(runner.execute("environment", ctx)["status"], "fail")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.artifacts import collect_artifacts

from runtime.common import digest, write_json

from runtime.broker import Broker


class ArtifactTests(unittest.TestCase):

    def test_test_evidence_must_match_real_cases_identity_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = {'artifact_host_dir': str(root / 'artifacts'), 'task_id': 'task-tests',
                       'target_sha': 'a' * 40, 'python_bin': '/opt/anchor-ci/runtime/task_python',
                       'task_venv': '/workspace/tasks/task-tests/run/venv'}
            broker = Broker({}, context, {}, root / 'output', lambda: False)
            for tool in ('frontend_tests', 'backend_tests'):
                output = root / 'artifacts' / tool
                output.mkdir(parents=True)
                junit = output / 'tests.xml'
                junit.write_text('<testsuites><testsuite><testcase name="actual-case"/></testsuite></testsuites>')
                valid = {key: context[key] for key in ('task_id', 'target_sha', 'task_venv')}
                valid.update(tool=tool, python_executable=context['python_bin'], junit_sha256=digest(junit),
                             tests=1, passed=1, failures=0, errors=0, skipped=0, selected_paths=['tests'])
                write_json(output / 'tests.json', valid)
                broker.verify_artifacts(tool)
                for field, bad in (('passed', 0), ('tests', True), ('target_sha', 'b' * 40),
                                   ('junit_sha256', 'c' * 64), ('tool', 'flaggems'),
                                   ('task_venv', '/workspace/tasks/other/run/venv'), ('selected_paths', [])):
                    with self.subTest(tool=tool, field=field), self.assertRaises(ValueError):
                        write_json(output / 'tests.json', {**valid, field: bad})
                        broker.verify_artifacts(tool)
                for outcome in ('skipped', 'failure', 'error'):
                    junit.write_text(f'<testsuites><testsuite><testcase><{outcome}/></testcase></testsuite></testsuites>')
                    with self.subTest(tool=tool, outcome=outcome), self.assertRaises(ValueError):
                        write_json(output / 'tests.json', {**valid, 'junit_sha256': digest(junit)})
                        broker.verify_artifacts(tool)


    def test_zero_exit_without_required_build_artifact_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = Broker({}, {'artifact_host_dir': str(root/'artifacts')}, {}, root/'output', lambda:False)
            with self.assertRaisesRegex(ValueError, 'without its required artifact'):
                broker.verify_artifacts('frontend_build')

    def test_preserves_tests_and_ir_while_reporting_omissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'source', root / 'output'
            (source / 'custom').mkdir(parents=True)
            (source / 'custom/repro.py').write_text('assert 1 == 1\n')
            (source / 'result.mlir').write_text('module {}\n')
            (source / 'wheel.whl').write_bytes(b'not-published')
            (source / 'oversize.log').write_text('x' * 256)
            (source / '.auth.json').write_text('{}')
            manifest = collect_artifacts(source, output, max_file_bytes=128)
            self.assertEqual({x['path'] for x in manifest['files']},
                             {'artifacts/custom/repro.py', 'artifacts/result.mlir'})
            self.assertEqual(len(manifest['omitted']), 3)
            for item in manifest['files']:
                self.assertEqual(digest(output / item['path']), item['sha256'])
            (source / 'custom/repro.py').write_text('later mutation')
            self.assertEqual((output / 'artifacts/custom/repro.py').read_text(), 'assert 1 == 1\n')

    def test_total_budget_reports_remaining_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            for name in ('a.txt', 'b.txt'):
                (source / name).write_text('0123456789')
            manifest = collect_artifacts(source, root / 'output', max_total_bytes=10)
            self.assertEqual(len(manifest['files']), 1)
            self.assertEqual(manifest['omitted'][0]['reason'], 'total publication limit')
