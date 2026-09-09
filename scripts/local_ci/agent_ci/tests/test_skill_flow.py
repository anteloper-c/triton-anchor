"""Local Skill flow through MCP/tools and independent one-way GitHub publication.

The single scripted Codex peer replaces the model. A fake Docker executable
provides fixture compiler/package probes; run_tool and contract_checks execute
unchanged. Gitee is a local bare repository, GitHub/Pages are recording peers.
This exercises orchestration and evidence, not compiler/backend correctness.
"""
from __future__ import annotations

import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
LOCAL_ROOT = HERE.parents[1]
sys.path.insert(0, str(LOCAL_ROOT))
sys.path.insert(0, str(HERE))
from agent_ci.executor import DockerExecutor
from agent_ci.protocol import canonical, current_key, metadata_digest, task_id
from agent_ci.skill import load_skill
from agent_ci.worker import Worker
from test_agent_ci import Fixture, git
from container_fixture import FAKE_DOCKER, VolumeManager


class MCPPeer:
    """A deterministic single model peer; every CI action uses stdio MCP."""

    def __init__(self, *, interrupt_once=False, reject_pr=False):
        self.interrupt_once, self.reject_pr = interrupt_once, reject_pr
        self.attempts = []
        self.environment_ids = []
        self.artifacts = []
        self.requests = []
        self.manifests = []
        self.prompts = []
        self.diagnostics = []
        self.active = 0
        self.max_active = 0

    def rpc(self, process, method, params=None):
        ident = len(self.requests) + 1
        request = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            request["params"] = params
        self.requests.append(request)
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        if not select.select([process.stdout], [], [], 40)[0]:
            raise TimeoutError("MCP peer timed out")
        response = json.loads(process.stdout.readline())
        if response.get("id") != ident or "error" in response:
            raise AssertionError(response)
        return response["result"]

    def call(self, process, tool_name, **arguments):
        reply = self.rpc(process, "tools/call", {"name": tool_name, "arguments": arguments})
        if reply.get("isError"):
            raise AssertionError(reply)
        return json.loads(reply["content"][0]["text"])

    def run(self, supervisor, service, recovery=""):
        # The production loader supplies the complete trusted bundle explicitly;
        # candidate repository SKILL.md files are never discovery inputs.
        bundle = load_skill()
        self.manifests.append(bundle.manifest)
        self.prompts.append(bundle.prompt)
        self.attempts.append(recovery)
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LOCAL_CI_RPC_SOCKET": str(service.path),
               "LOCAL_CI_RPC_TOKEN": service.token}
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        process = subprocess.Popen([sys.executable, "-u", str(LOCAL_ROOT / "agent_ci/mcp_server.py")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        try:
            initialized = self.rpc(process, "initialize", {"protocolVersion": "2024-11-05"})
            assert initialized["serverInfo"]["name"] == "local-ci"
            listing = self.rpc(process, "tools/list")
            assert {item["name"] for item in listing["tools"]} >= {"context", "start_check", "finish"}
            context = self.call(process, "context")
            assert context["policy"]["required_checks"] == ["environment", "contract_tests"]
            assert context["diagnostics"]["candidate"]["diagnostic_requires"] == []
            early = self.call(process, "run_custom", name="inspect_runtime.py", language="python",
                              content="import sys\nprint('diagnostic Python:', sys.executable)\n",
                              reason="Inspect this task before environment or build checks pass")
            diagnostic = self.call(process, "poll_check", execution_id=early["execution_id"], wait_seconds=30)
            assert diagnostic["status"] == "pass", diagnostic
            assert diagnostic["custom_mode"] == "diagnostic" and not diagnostic["required"], diagnostic
            self.diagnostics.append(diagnostic)
            for tool in context["policy"]["required_checks"]:
                started = self.call(process, "start_check", tool_id=tool,
                                    reason="Follow the Skill and the actual mandatory impact policy")
                record = self.call(process, "poll_check", execution_id=started["execution_id"], wait_seconds=30)
                assert record["status"] in {"pass", "fail"}, record
                if tool == "environment":
                    self.environment_ids.append(record["execution_id"])
                    if self.interrupt_once and len(self.attempts) == 1:
                        raise OSError("Simulated model disconnect after saved environment evidence")
                self.artifacts.append(self.call(process, "read_artifact", execution_id=record["execution_id"]))
                if record["status"] == "fail":
                    break
            source = self.call(process, "read_file", path="docs/flow.md")
            assert "flow.md" == Path(source["path"]).name
            self.call(process, "submit_review", kind="pr_info", status="fail" if self.reject_pr else "pass",
                      summary="Missing required reproduction detail" if self.reject_pr else "The frozen PR and change intent agree",
                      evidence=[])
            self.call(process, "submit_review", kind="architecture", status="pass",
                      summary="Documentation change preserves the referenced architecture contract",
                      evidence=[{"rule_id": "abi-isolation", "path": "python/triton_anchor/adapters/base.py", "line": 1}])
            self.call(process, "finish", summary="Model wording cannot override recorded tool or review failures")
            return {"exit_code": 0}
        finally:
            self.active -= 1
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            error = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            if process.returncode:
                raise AssertionError(error)


class GitHubPeer:
    def __init__(self, task):
        self.task, self.repository, self.calls = task, task["repository"], []
        self.latest_status, self.latest_comment = None, None

    def request(self, path):
        task = self.task
        if path == "pulls/7":
            return {"title": task["title"], "body": task["description"], "labels": [], "state": "open", "draft": False,
                    "head": {"sha": task["head_sha"]}, "base": {"ref": task["target_branch"]}}
        if path == "git/ref/pull/7/merge":
            return {"object": {"sha": task["tested_sha"]}}
        raise AssertionError("Unexpected external request: " + path)

    optional = request

    def status(self, task, state, description, url=""):
        self.calls.append(("status", state))
        self.latest_status = (task["tested_sha"], state, description)

    def status_matches(self, task, state, description):
        return self.latest_status == (task["tested_sha"], state, description)

    def comment(self, task, body):
        if self.latest_comment == body:
            return False
        self.calls.append(("comment", body))
        self.latest_comment = body
        return True


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0, "Linux root required for isolated test UIDs")
class SkillFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-skill-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.fixture = Fixture(self.root)
        self.fake_docker = self.root / "docker-fixture"
        self.fake_docker.write_text(FAKE_DOCKER)
        self.fake_docker.chmod(0o755)
        self.control = self.root / "control"
        tools = self.control / "scripts/local_ci/tools"
        tools.mkdir(parents=True)
        for name in ("run_tool.sh", "run_tool.py", "contract_checks.py"):
            shutil.copyfile(LOCAL_ROOT / "tools" / name, tools / name)
        self.probes = self.root / "probes"
        self.probes.mkdir()
        for name in ("cmake", "ninja", "c++", "uv"):
            path = self.probes / name
            path.write_text("#!/bin/sh\nprintf 'fixture dependency probe\\n'\n")
            path.chmod(0o755)
        self.llvm = self.root / "llvm"
        for directory in ("bin", "include/llvm", "include/mlir", "lib"):
            (self.llvm / directory).mkdir(parents=True, exist_ok=True)
        binary = self.llvm / "bin/llvm-config"
        binary.write_text("#!/bin/sh\nprintf 'fixture-llvm-version\\n'\n")
        binary.chmod(0o755)

    def worker(self, *, conflict=False, reject_pr=False, interrupt_once=False):
        fixture = self.fixture
        base = fixture.task["tested_sha"]
        git(fixture.source, "checkout", "-qb", "docs-flow")
        (fixture.source / "docs").mkdir()
        content = "<<<<<<< unresolved\n" if conflict else "The documented CI flow preserves the contract.\n"
        (fixture.source / "docs/flow.md").write_text(content)
        git(fixture.source, "add", ".")
        git(fixture.source, "commit", "-qm", "documentation fixture")
        head = git(fixture.source, "rev-parse", "HEAD")
        git(fixture.source, "checkout", "-q", "main")
        git(fixture.source, "merge", "--no-ff", "-qm", "docs merge fixture", head)
        tested = git(fixture.source, "rev-parse", "HEAD")
        git(fixture.bare, "fetch", str(fixture.source), "main", "docs-flow")
        task = fixture.task
        task.update(base_sha=base, head_sha=head, tested_sha=tested,
                    title="docs: clarify the CI flow", description="Purpose: document the CI flow; validation: changed-document contract checks.")
        task["metadata_digest"] = metadata_digest(task)
        task["task_id"] = task_id(task)
        for key, sha in (("task_ref", tested), ("base_task_ref", base), ("head_task_ref", head)):
            git(fixture.bare, "update-ref", "refs/heads/" + task[key], sha)
        fixture.relay.write("local-ci-control", {
            f"tasks/{task['task_id']}.json": canonical(task),
            f"current/{current_key(task)}.json": canonical({"task_id": task["task_id"]}),
        })
        fixture.relay.refresh()
        root, probes, llvm = self.root, self.probes, self.llvm

        class Manager(VolumeManager):
            def acquire(self, *args):
                generation = super().acquire(*args)
                generation["env"] = {"SEED_PYTHON": sys.executable, "LLVM_BUILD_DIR": str(llvm),
                                     "PATH": str(probes) + ":" + os.defpath, "PACKAGE_TOOL": "uv", "LOCAL_CI_MIN_FREE_BYTES": "0"}
                return generation

        self.peer = MCPPeer(interrupt_once=interrupt_once, reject_pr=reject_pr)
        self.manager = Manager(root, backend=False, docker_bin=self.fake_docker, seed_metadata=True)
        config = {"state_dir": str(root / "state"), "simulation": True, "codex_attempts": 2 if interrupt_once else 1,
                  "retry_delay_seconds": 0, "rpc_socket_dir": str(root / "rpc"), "docker_bin": str(self.fake_docker),
                  "runtime": {"kind": "docker-rootless", "endpoint": "unix:///run/user/1000/docker.sock"},
                  "container_control_root": str(self.control)}
        return Worker(config, relay=fixture.relay, manager=self.manager, driver=self.peer, executor_factory=DockerExecutor)

    def result(self, worker):
        task = self.fixture.task
        row = worker.journal.task(task["task_id"])
        self.assertEqual("complete", row["phase"])
        self.assertEqual("gitee_upload", json.loads(row["detail"])["completion_boundary"])
        box = worker.journal.outbox(task["task_id"])
        self.assertIsNotNone(box["published"])
        self.assertNotIn("receipt", box)
        result_path = Path(box["payload_path"])
        self.fixture.relay.refresh()
        remote = self.fixture.relay.read("local-ci-results", f"runs/v4/{task['task_id']}/{row['run_id']}/result.json")
        self.assertEqual(result_path.read_bytes(), remote)
        return json.loads(result_path.read_text()), result_path

    def github(self):
        spec = importlib.util.spec_from_file_location("skill_flow_gateway", LOCAL_ROOT.parent / "ci/gateway_v4.py")
        gateway = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = gateway
        spec.loader.exec_module(gateway)
        task = self.fixture.task
        gh = GitHubPeer(task)
        control = gateway.GitStore(str(self.fixture.bare), "local-ci-control")
        results = gateway.GitStore(str(self.fixture.bare), "local-ci-results")
        self.addCleanup(control.close)
        self.addCleanup(results.close)
        return gateway, gh, control, results

    def no_receipts(self):
        paths = git(self.fixture.bare, "ls-tree", "-r", "--name-only", "refs/heads/local-ci-control").splitlines()
        self.assertFalse(any(path.startswith("receipts/") for path in paths))

    def publish_on_github(self, worker, expected_status):
        task = self.fixture.task
        # The task is already complete while GitHub and Pages have done nothing.
        self.assertEqual("complete", worker.journal.task(task["task_id"])["phase"])
        attempts = list(self.peer.attempts)
        control_head = git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-control")
        gateway, gh, control, results = self.github()
        self.assertEqual([], gh.calls)
        self.assertFalse((self.root / "dashboard").exists())
        published = gateway.collect_results(gh, control, results, self.root / "dashboard")
        self.assertEqual(1, len(published))
        self.assertEqual(task["tested_sha"], published[0]["tested_sha"])
        self.assertEqual(("status", expected_status), gh.calls[0])
        self.assertEqual("comment", gh.calls[1][0])
        # Preserve the existing GitHub status -> comment -> Pages order.
        self.assertTrue((self.root / "dashboard").is_dir())
        (self.root / "pages-simulation.json").write_text(json.dumps({"published": True}))
        before = list(gh.calls)
        self.assertEqual([], gateway.collect_results(gh, control, results, self.root / "dashboard"))
        self.assertEqual(before, gh.calls)
        self.no_receipts()
        self.assertEqual(control_head, git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-control"))
        self.assertEqual("complete", worker.journal.task(task["task_id"])["phase"])
        worker.scan()
        self.assertEqual(attempts, self.peer.attempts)

    def test_skill_resumes_checks_and_completes_on_upload_before_github(self):
        worker = self.worker(interrupt_once=True)
        worker.scan()
        result, path = self.result(worker)
        self.assertEqual("pass", result["status"], result)
        self.assertEqual(2, len(self.peer.attempts))
        self.assertTrue(self.peer.attempts[1])
        self.assertEqual(1, self.peer.max_active)
        self.assertEqual(1, len(set(self.peer.environment_ids)))
        self.assertEqual(self.peer.manifests[0]["digest"], self.peer.manifests[1]["digest"])
        self.assertEqual(load_skill().prompt, self.peer.prompts[0])
        self.assertEqual("SKILL.md", self.peer.manifests[0]["entrypoint"])
        self.assertEqual(2, len(self.peer.diagnostics))
        self.assertTrue(all(record["dependency_executions"] == {} for record in self.peer.diagnostics))
        checks = {row["tool_id"]: row for row in result["checks"]}
        contract = checks["contract_tests"]
        self.assertEqual("pass", contract["status"])
        evidence = json.loads((path.parent / "evidence" / contract["execution_id"] / "contracts.json").read_text())
        self.assertEqual(self.fixture.task["tested_sha"], evidence["tested_sha"])
        self.assertEqual("docs/flow.md", evidence["verified_files"][0]["path"])
        self.assertIn("no_conflict_markers", evidence["verified_files"][0]["checks"])
        self.assertTrue(any("contract_checks.py" in item["content"] for item in self.peer.artifacts))
        self.publish_on_github(worker, "success")

    def test_real_contract_failure_overrides_model_summary_and_is_delivered(self):
        worker = self.worker(conflict=True)
        worker.scan()
        result, path = self.result(worker)
        self.assertEqual("fail", result["status"], result)
        self.assertTrue(any(row["kind"] == "check_failure" and row["tool_id"] == "contract_tests" for row in result["blockers"]))
        contract = next(row for row in result["checks"] if row["tool_id"] == "contract_tests")
        self.assertNotEqual(0, contract["exit_code"])
        logs = "\n".join(item.read_text() for item in (path.parent / "evidence" / contract["execution_id"]).glob("*.log"))
        # Git's whitespace/conflict check rejects the candidate before the
        # Markdown-specific parser; the retained command identifies both SHAs.
        self.assertIn("'diff', '--check'", logs)
        self.assertIn("non-zero exit status 2", logs)
        self.assertIn(self.fixture.task["tested_sha"], logs)
        self.publish_on_github(worker, "failure")

    def test_pr_information_failure_blocks_passing_real_tools(self):
        worker = self.worker(reject_pr=True)
        worker.scan()
        result, _ = self.result(worker)
        self.assertEqual("fail", result["status"], result)
        required = {item["tool_id"]: item["status"] for item in result["checks"] if item.get("required")}
        self.assertEqual({"environment": "pass", "contract_tests": "pass"}, required)
        self.assertTrue(any(item["kind"] == "pr_information" for item in result["blockers"]))
        self.publish_on_github(worker, "failure")

    def test_upload_failure_retries_sealed_evidence_without_codex_resume(self):
        worker = self.worker()
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=OSError("fixture Gitee upload outage")):
            worker.scan()
        task_id_value = self.fixture.task["task_id"]
        self.assertEqual("publishing", worker.journal.task(task_id_value)["phase"])
        box = worker.journal.outbox(task_id_value)
        original = Path(box["payload_path"]).read_bytes()
        executions = worker.journal.executions(task_id_value)
        self.assertEqual(1, len(self.peer.attempts))
        worker.scan()
        _, path = self.result(worker)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(box["digest"], worker.journal.outbox(task_id_value)["digest"])
        self.assertEqual(executions, worker.journal.executions(task_id_value))
        self.assertEqual(1, len(self.peer.attempts))
        self.no_receipts()
        self.publish_on_github(worker, "success")

    def test_lost_upload_response_retries_immutable_same_run_without_codex(self):
        worker = self.worker()
        publish = self.fixture.relay.publish_result
        def lost_response(*args):
            publish(*args)
            raise OSError("fixture connection lost after immutable Git push")
        with mock.patch.object(self.fixture.relay, "publish_result", side_effect=lost_response):
            worker.scan()
        task_id_value = self.fixture.task["task_id"]
        row = worker.journal.task(task_id_value)
        self.assertEqual("publishing", row["phase"])
        before = git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-results")
        worker.scan()
        self.result(worker)
        self.assertEqual(before, git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-results"))
        self.assertEqual(row["run_id"], worker.journal.task(task_id_value)["run_id"])
        self.assertEqual(1, len(self.peer.attempts))
        self.no_receipts()

    def test_github_comment_retries_independently_of_completed_worker(self):
        worker = self.worker()
        worker.scan()
        self.result(worker)
        gateway, gh, control, results = self.github()
        with mock.patch.object(gh, "comment", side_effect=OSError("fixture GitHub comment outage")):
            gateway.collect_results(gh, control, results, self.root / "dashboard")
        published = gateway.collect_results(gh, control, results, self.root / "dashboard")
        self.assertEqual(1, len(published))
        self.assertEqual("comment", gh.calls[-1][0])
        self.assertEqual("complete", worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.assertEqual(1, len(self.peer.attempts))
        self.no_receipts()

    def test_pages_failure_does_not_reopen_worker_or_delay_github_status(self):
        worker = self.worker()
        worker.scan()
        self.result(worker)
        gateway, gh, control, results = self.github()
        self.assertEqual(1, len(gateway.collect_results(gh, control, results, self.root / "dashboard")))
        self.assertEqual(("status", "success"), gh.calls[0])
        self.assertEqual("comment", gh.calls[1][0])
        (self.root / "pages-simulation.json").write_text(json.dumps({"published": False, "error": "fixture Pages outage"}))
        worker.scan()
        self.assertEqual("complete", worker.journal.task(self.fixture.task["task_id"])["phase"])
        self.assertEqual(1, len(self.peer.attempts))
        self.assertEqual("success", gh.latest_status[1])
        self.no_receipts()


if __name__ == "__main__":
    unittest.main()
