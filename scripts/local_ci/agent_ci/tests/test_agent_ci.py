from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LOCAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_ROOT))
from agent_ci.policy import TOOLS, minimum_checks
from agent_ci.protocol import TASK_SCHEMA, RECEIPT_SCHEMA, ContractError, canonical, current_key, metadata_digest, task_id, validate_task
from agent_ci.relay import GitRelay
from agent_ci.supervisor import Supervisor, ToolService
from agent_ci.worker import Worker


def git(cwd, *args):
    return subprocess.check_output(["git", "-c", "user.name=Simulation", "-c", "user.email=simulation@example.invalid", *args], cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.source = root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        (self.source / "triton/cmake").mkdir(parents=True)
        (self.source / "triton/cmake/llvm-hash.txt").write_text("a" * 40 + "\n")
        (self.source / "triton/python/triton").mkdir(parents=True)
        (self.source / "triton/python/triton/__init__.py").write_text('__version__ = "3.0.0"\n')
        (self.source / "README.md").write_text("base documentation\n")
        (self.source / "behavior.py").write_text("VALUE = 0\n")
        for relative in ("adapters/base.py", "anchor_ir.py", "pipeline.py", "extensions/base.py"):
            path = self.source / "python/triton_anchor" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# preserve documented architecture contract\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        base = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "-qb", "change")
        (self.source / "behavior.py").write_text("VALUE = 1\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "candidate")
        head = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "-q", "main")
        git(self.source, "merge", "--no-ff", "-qm", "test merge", head)
        tested = git(self.source, "rev-parse", "HEAD")
        self.bare = root / "relay.git"
        git(root, "clone", "--bare", "--quiet", str(self.source), str(self.bare))
        self.task = {
            "schema": TASK_SCHEMA, "repository": "likehupochuan/triton-anchor", "event_kind": "pull_request", "pr_number": 7,
            "task_ref": "ci/pr-7/change", "base_task_ref": "ci/base/pr-7/change", "head_task_ref": "ci/head/pr-7/change",
            "tested_sha": tested, "base_sha": base, "head_sha": head, "worker_revision_sha": base,
            "target_branch": "main", "title": "Verify the candidate", "description": "A simulation of CI, not a real backend test",
            "labels": [], "state": "open", "draft": False, "captured_at": "2026-09-07T00:00:00Z", "llvm_hash": "a" * 40, "full": False,
        }
        self.task["metadata_digest"] = metadata_digest(self.task)
        self.task["task_id"] = task_id(self.task)
        self.relay = GitRelay(str(self.bare), root / "relay-cache", allow_local=True)
        for name, value in (("task_ref", tested), ("base_task_ref", base), ("head_task_ref", head)):
            git(self.bare, "update-ref", "refs/heads/" + self.task[name], value)
        self.relay.write("local-ci-control", {
            f"tasks/{self.task['task_id']}.json": canonical(self.task),
            f"current/{current_key(self.task)}.json": canonical({"task_id": self.task["task_id"]}),
        })
        self.relay.refresh()


class FakeManager:
    def __init__(self, root, backend=True):
        self.root, self.backend, self.acquired, self.released = root, backend, [], []

    def acquire(self, task_id, target_branch, llvm_hash):
        self.acquired.append(task_id)
        return {"profile": "simulated-3.0" if self.backend else "simulated-frontend", "generation": "simulation-1",
                "workspace_host": str(self.root / "workspace"), "workspace_container": "/workspace",
                "container": "persistent-simulation", "environment_fingerprint": "f" * 64, "backend_enabled": self.backend, "env": {}}

    def release(self, task_id):
        self.released.append(task_id)


class FakeExecutor:
    """Only the Docker/build boundary is fake; custom tests are actual subprocesses."""
    calls = []
    failures = {}

    def __init__(self, config, state_dir, generation, task, relay):
        self.config, self.generation, self.task, self.relay = config, generation, task, relay
        self.root = Path(generation["workspace_host"]) / task["task_id"]

    def prepare(self, variant="candidate"):
        path = self.root / variant
        path.parent.mkdir(parents=True, exist_ok=True)
        self.relay.checkout(self.task["tested_sha" if variant == "candidate" else "base_sha"], path)
        return path

    def stop(self, execution_id):
        self.calls.append(("stop", execution_id))

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        self.calls.append((tool_id, variant, parameters))
        artifact = self.root / "artifacts" / execution_id
        artifact.mkdir(parents=True)
        status, code, reason = "pass", 0, "simulated external tool"
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "environment_fingerprint": self.generation["environment_fingerprint"], "artifact_dir": str(artifact)}
        if custom:
            source = artifact / custom["name"]
            source.write_text(custom["content"])
            process = subprocess.run([sys.executable if custom["language"] == "python" else "bash", str(source)], cwd=self.prepare(variant), capture_output=True)
            code, status = process.returncode, "pass" if process.returncode == 0 else "fail"
            record["script_digest"] = hashlib.sha256(custom["content"].encode()).hexdigest()
            (artifact / "execution.log").write_bytes(process.stdout + process.stderr)
        else:
            if tool_id in self.failures and self.failures[tool_id]:
                status, reason = self.failures[tool_id].pop(0)
                code = 137 if reason == "oom" else 1
            (artifact / "execution.log").write_text("SIMULATED external build boundary\n")
        if cancelled.is_set():
            status = "cancelled"
        return {**record, "status": status, "exit_code": code, "reason": reason, "duration_seconds": 0.01}


class FakeCodex:
    def __init__(self):
        self.calls = 0

    def run(self, supervisor, service, recovery=""):
        self.calls += 1
        context = supervisor.context()
        if context.get("phase") == "publication_recovery":
            supervisor.retry_publication("Retry the existing immutable result after simulated outage")
            return {"exit_code": 0, "publication_recovery": True}
        # Select from the actual policy rather than a hard-coded full pipeline.
        for tool in context["policy"]["required_checks"]:
            started = supervisor.start_check(tool, "Required by inspected change scope")
            result = supervisor.poll_check(started["execution_id"], 30)
            if result["status"] != "pass":
                break
        supervisor.submit_review("pr_info", "pass", "Simulation metadata matches the frozen change", [])
        supervisor.submit_review("architecture", "pass", "No contract violation in the inspected fixture", [{"rule_id": "abi-isolation", "path": "python/triton_anchor/adapters/base.py", "line": 1}])
        supervisor.finish("SIMULATION: control logic executed, real compiler/backend not exercised")
        return {"exit_code": 0}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root)
        self.config = {"state_dir": str(self.root / "state"), "simulation": True, "codex_attempts": 1, "retry_delay_seconds": 0, "rpc_socket_dir": str(self.root / "rpc")}
        self.manager, self.driver = FakeManager(self.root), FakeCodex()
        FakeExecutor.calls, FakeExecutor.failures = [], {}

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, backend=True):
        self.manager.backend = backend
        return Worker(self.config, relay=self.fixture.relay, manager=self.manager, driver=self.driver, executor_factory=FakeExecutor)

    def receipt(self, worker, **overrides):
        row = worker.journal.task(self.fixture.task["task_id"])
        box = worker.journal.outbox(self.fixture.task["task_id"])
        document = {"schema": RECEIPT_SCHEMA, "task_id": self.fixture.task["task_id"], "run_id": row["run_id"],
                    "tested_sha": self.fixture.task["tested_sha"], "result_digest": box["digest"], "status": "complete",
                    "github_status": True, "comment": True, "dashboard": True, **overrides}
        self.fixture.relay.write("local-ci-control", {f"receipts/{self.fixture.task['task_id']}/{row['run_id']}.json": canonical(document)})

    def test_complete_only_after_matching_full_receipt(self):
        worker = self.worker()
        worker.scan()
        row = worker.journal.task(self.fixture.task["task_id"])
        self.assertEqual(row["phase"], "awaiting_receipt")
        result = json.loads(Path(worker.journal.outbox(row["task_id"])["payload_path"]).read_text())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["required_checks"], list(TOOLS))
        self.receipt(worker)
        worker.deliver(row)
        self.assertEqual(worker.journal.task(row["task_id"])["phase"], "complete")

    def test_worker_through_actual_receiver_and_git_receipt(self):
        spec = importlib.util.spec_from_file_location("v4_integrated_receiver", LOCAL_ROOT.parent / "ci/gateway_v4.py")
        gateway = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = gateway
        spec.loader.exec_module(gateway)
        task = self.fixture.task

        class FakeGitHub:
            repository = task["repository"]
            calls = []

            def request(self, path):
                if path == "pulls/7":
                    return {"title": task["title"], "body": task["description"], "labels": [], "state": "open", "draft": False,
                            "head": {"sha": task["head_sha"]}, "base": {"ref": task["target_branch"]}}
                if path == "git/ref/pull/7/merge":
                    return {"object": {"sha": task["tested_sha"]}}
                raise AssertionError(path)

            optional = request

            def status(self, task, state, description, url=""):
                self.calls.append(("status", state))

            def comment(self, task, body):
                self.calls.append(("comment", body))

        worker = self.worker()
        worker.scan()
        control = gateway.GitStore(str(self.fixture.bare), "local-ci-control")
        results = gateway.GitStore(str(self.fixture.bare), "local-ci-results")
        self.addCleanup(control.close)
        self.addCleanup(results.close)
        gh = FakeGitHub()
        receipts = gateway.collect_results(gh, control, results, self.root / "dashboard")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(gh.calls[0], ("status", "success"))
        self.assertEqual(gh.calls[1][0], "comment")
        worker.deliver(worker.journal.task(task["task_id"]))
        self.assertEqual(worker.journal.task(task["task_id"])["phase"], "awaiting_receipt")
        # Pages is the external boundary; ACK is run only after its mocked success.
        gateway.acknowledge(gh, control, results, receipts)
        worker.deliver(worker.journal.task(task["task_id"]))
        self.assertEqual(worker.journal.task(task["task_id"])["phase"], "complete")
        self.assertEqual(gateway.collect_results(gh, control, results, self.root / "dashboard"), [])

    def test_worker_revision_mismatch_is_reported_before_environment(self):
        worker = self.worker()
        with patch("agent_ci.worker.validate_control_revision", side_effect=ContractError("installed revision differs")):
            worker.scan()
        self.assertFalse(self.manager.acquired)
        result = json.loads(Path(worker.journal.outbox(self.fixture.task["task_id"])["payload_path"]).read_text())
        self.assertEqual(result["status"], "infra_error")
        self.assertIn("installed revision differs", result["unfinished"])

    def test_api_failure_is_bounded_and_explicit_resume_reuses_success(self):
        worker = self.worker()
        self.config["codex_attempts"] = 3
        attempts = []

        def interrupted(supervisor, service, recovery=""):
            attempts.append(1)
            started = supervisor.start_check("environment", "Keep verified preparation across recovery")
            supervisor.poll_check(started["execution_id"], 30)
            raise OSError("simulated company API interruption")

        with patch.object(self.driver, "run", side_effect=interrupted):
            worker.scan()
        self.assertEqual(len(attempts), 3)
        ident = self.fixture.task["task_id"]
        self.receipt(worker)
        worker.deliver(worker.journal.task(ident))
        self.assertEqual(worker.journal.task(ident)["phase"], "incomplete")
        before = worker.journal.task(ident)["run_id"]
        worker.journal.resume(ident)
        worker.scan()
        self.assertNotEqual(before, worker.journal.task(ident)["run_id"])
        self.assertEqual(len([c for c in FakeExecutor.calls if c[0] == "environment"]), 1)

    def test_rebuilt_dependency_invalidates_downstream_evidence(self):
        supervisor = self.supervisor()
        for tool in TOOLS[:4]:
            supervisor.poll_check(supervisor.start_check(tool, "Build verified frontend")["execution_id"], 30)
        self.assertTrue(supervisor.fresh("frontend_smoke"))
        supervisor.poll_check(supervisor.start_check("frontend_build", "Rebuild with a changed option", force=True)["execution_id"], 30)
        self.assertFalse(supervisor.fresh("frontend_smoke"))
        with self.assertRaises(ContractError):
            supervisor.start_check("backend_rebuild", "Stale frontend must not be consumed")

    def test_execution_setup_failure_is_a_terminal_infrastructure_record(self):
        supervisor = self.supervisor()
        with patch.object(supervisor.executor, "run", side_effect=OSError("No space left on device")):
            result = supervisor.poll_check(supervisor.start_check("environment", "Preflight storage")["execution_id"], 30)
        self.assertEqual(result["status"], "infra_error")
        report = supervisor.finish("Unable to prepare execution storage")
        self.assertEqual(report["status"], "infra_error")
        self.assertFalse(any(r["status"] in {"queued", "running"} for r in report["checks"]))

    def test_real_trusted_checkout_revision_and_dirty_policy_are_rejected(self):
        from agent_ci import control
        config = {"control_root": str(self.fixture.source)}
        with patch.object(control, "__file__", str(self.fixture.source / "scripts/local_ci/agent_ci/control.py")):
            with self.assertRaises(ContractError):
                control.validate_control_revision(config, self.fixture.task)
            git(self.fixture.source, "checkout", "--detach", self.fixture.task["worker_revision_sha"])
            self.assertEqual(control.validate_control_revision(config, self.fixture.task), self.fixture.task["worker_revision_sha"])
            policy = self.fixture.source / "scripts/local_ci/policy.py"
            policy.parent.mkdir(parents=True)
            policy.write_text("# uncommitted control changes\n")
            with self.assertRaises(ContractError):
                control.validate_control_revision(config, self.fixture.task)

    def test_architecture_blocking_requires_real_contract_and_changed_quote(self):
        supervisor = self.supervisor()
        finding = {"category": "architecture", "summary": "Fixture contract violation", "path": "behavior.py", "line": 1,
                   "rule_id": "abi-isolation", "violation_evidence": {"rule_line": 1,
                       "rule_quote": "preserve documented architecture contract", "code_quote": "VALUE = 1", "explanation": "fixture evidence"}}
        self.assertTrue(supervisor.finding_blocker(finding))
        self.assertFalse(supervisor.finding_blocker({**finding, "violation_evidence": {**finding["violation_evidence"], "rule_quote": "invented contract"}}))
        self.assertFalse(supervisor.finding_blocker({**finding, "path": "README.md"}))

    def test_publication_failure_reuses_completed_work_after_restart(self):
        worker = self.worker()
        with patch.object(self.fixture.relay, "publish_result", side_effect=OSError("simulated publish outage")):
            worker.scan()
        calls = list(FakeExecutor.calls)
        replacement = self.worker()
        replacement.scan()
        self.assertEqual(FakeExecutor.calls, calls)
        self.assertEqual(self.driver.calls, 2)  # One build session and one publication-only recovery.
        self.assertEqual(replacement.journal.task(self.fixture.task["task_id"])["phase"], "awaiting_receipt")

    def test_stale_or_partial_receipt_cannot_complete(self):
        worker = self.worker()
        worker.scan()
        self.receipt(worker, dashboard=False)
        with self.assertRaises(ContractError):
            worker.deliver(worker.journal.task(self.fixture.task["task_id"]))

    def test_frontend_only_is_explicit_not_applicable(self):
        worker = self.worker(backend=False)
        worker.scan()
        result = json.loads(Path(worker.journal.outbox(self.fixture.task["task_id"])["payload_path"]).read_text())
        self.assertEqual(result["required_checks"], list(TOOLS[:4]))
        self.assertEqual({r["tool_id"] for r in result["checks"] if r["status"] == "not_applicable"}, set(TOOLS[4:]))

    def test_oom_retries_only_one_tool_with_lower_parallelism(self):
        FakeExecutor.failures = {"frontend_build": [("infra_error", "oom")]}
        worker = self.worker()
        worker.scan()
        builds = [r for r in FakeExecutor.calls if r[0] == "frontend_build"]
        self.assertEqual(len(builds), 2)
        self.assertEqual(builds[1][2]["max_jobs"], 4)
        self.assertEqual(len([r for r in FakeExecutor.calls if r[0] == "environment"]), 1)

    def test_invalid_identity_and_llvm_snapshot_do_not_execute(self):
        task = {**self.fixture.task, "tested_sha": "b" * 40}
        with self.assertRaises(ContractError):
            validate_task(task)
        task = {**self.fixture.task, "llvm_hash": "b" * 40}
        self.assertFalse(self.fixture.relay.validity(task)[0])

    def test_cancel_does_not_accept_old_result(self):
        worker = self.worker()
        worker.scan()
        ident = self.fixture.task["task_id"]
        self.fixture.relay.write("local-ci-control", {f"cancel/{ident}.json": canonical({"task_id": ident, "reason": "new commit"})})
        worker.deliver(worker.journal.task(ident))
        self.assertEqual(worker.journal.task(ident)["phase"], "cancelled")

    def supervisor(self):
        worker = self.worker()
        worker.journal.register(self.fixture.task)
        generation = self.manager.acquire(self.fixture.task["task_id"], "main", "a" * 40)
        executor = FakeExecutor(self.config, worker.state_dir, generation, self.fixture.task, self.fixture.relay)
        policy = minimum_checks([{"path": "behavior.py"}], backend_enabled=True)
        supervisor = Supervisor(self.fixture.task, policy, worker.journal, executor, self.root / "run", changes=[{"path": "behavior.py"}])
        self.addCleanup(supervisor.close)
        return supervisor

    def test_cannot_skip_required_checks_or_dependencies(self):
        supervisor = self.supervisor()
        with self.assertRaises(ContractError):
            supervisor.start_check("frontend_smoke", "Try to skip prerequisites")
        report = supervisor.finish("Missing checks")
        self.assertEqual(report["status"], "infra_error")
        self.assertIn("environment", report["unfinished"])

    def test_generated_reproduction_needs_candidate_repeat_and_passing_base(self):
        supervisor = self.supervisor()
        for variant in ("candidate", "base"):
            supervisor.poll_check(supervisor.start_check("environment", "prepare", variant=variant)["execution_id"], 30)
        script = "import pathlib\nexec(pathlib.Path('behavior.py').read_text())\nassert VALUE == 0\n"
        ids = []
        for variant in ("candidate", "candidate", "base"):
            result = supervisor.run_custom("reproduce.py", script, "python", "Determine candidate causality", variant, source_only=True)
            supervisor.poll_check(result["execution_id"], 30)
            ids.append(result["execution_id"])
        finding = {"severity": "high", "summary": "Candidate changes expected value", "path": "behavior.py", "line": 1, "execution_ids": ids}
        self.assertTrue(supervisor.finding_blocker(finding))
        self.assertFalse(supervisor.finding_blocker({**finding, "execution_ids": ids[:2]}))

    def test_mcp_stdio_and_task_scoped_permission(self):
        supervisor = self.supervisor()
        with ToolService(supervisor, self.root / "mcp.sock") as service:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "context", "arguments": {}}}
            environment = {**os.environ, "LOCAL_CI_RPC_SOCKET": str(service.path), "LOCAL_CI_RPC_TOKEN": service.token}
            result = subprocess.run([sys.executable, str(LOCAL_ROOT / "agent_ci/mcp_server.py")], input=json.dumps(request) + "\n", capture_output=True, text=True, env=environment, check=True)
            reply = json.loads(result.stdout)
            self.assertFalse(reply["result"]["isError"])
            environment["LOCAL_CI_RPC_TOKEN"] = "incorrect"
            result = subprocess.run([sys.executable, str(LOCAL_ROOT / "agent_ci/mcp_server.py")], input=json.dumps(request) + "\n", capture_output=True, text=True, env=environment, check=True)
            self.assertTrue(json.loads(result.stdout)["result"]["isError"])

    def test_paths_and_task_scripts_cannot_escape(self):
        supervisor = self.supervisor()
        with self.assertRaises(ContractError):
            supervisor.read_file("../../outside")
        with self.assertRaises(ContractError):
            supervisor.run_custom("../bad.py", "pass", "python", "bad")

    def test_policy_cannot_treat_program_or_renamed_code_as_docs(self):
        program = minimum_checks([{"path": "scripts/local_ci/skills/local-ci/references/AI_CI_PROGRAM.md"}], backend_enabled=True)
        self.assertIn("contract_tests", program["required_checks"])
        renamed = minimum_checks([{"path": "docs/example.md", "old_path": "csrc/old.cpp", "status": "R100"}], backend_enabled=True)
        self.assertEqual(renamed["required_checks"], list(TOOLS))
        with self.assertRaises(ContractError):
            minimum_checks([], backend_enabled=True)


if __name__ == "__main__":
    unittest.main()
