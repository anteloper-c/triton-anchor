from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

LOCAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_ROOT))
from agent_ci.policy import TOOLS, changed_files, minimum_checks
from agent_ci.protocol import TASK_SCHEMA, ContractError, canonical, current_key, metadata_digest, task_id, validate_task
from agent_ci.relay import GitRelay
from agent_ci.state import Journal
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
        self.registry, self._leases, self.quarantined = {}, {}, []
        self.validation_failure = None

    def acquire(self, task_id, target_branch, llvm_hash):
        self.acquired.append(task_id)
        ident = uuid.uuid4().hex
        generation = {"profile": "simulated-3.0" if self.backend else "simulated-frontend", "generation": ident,
                "attempt_id": ident, "task_id": task_id, "run_id": "simulation-run", "image_release_id": "image-1",
                "container_id": "container-" + ident, "workspace_host": str(self.root / "state/task-staging" / task_id / ident),
                "workspace_container": "/task", "container": "task-" + ident,
                "environment_fingerprint": "f" * 64, "backend_enabled": self.backend, "env": {}}
        self.registry[generation["generation"]] = generation
        self._leases[task_id] = {"generation": generation["generation"]}
        return generation

    def acquire_task(self, task, run_id, *, rpc_directory):
        for handle in self.registry.values():
            if handle["task_id"] == task["task_id"] and handle["run_id"] == run_id and not handle.get("removed"):
                return handle
        handle = self.acquire(task["task_id"], task["target_branch"], task["llvm_hash"])
        handle.update(run_id=run_id, rpc_host_dir=str(rpc_directory), rpc_container_dir="/run/local-ci-rpc")
        return handle

    def recover_task(self, handle):
        return {"status": "rebuild_required" if handle.get("removed") else "same_attempt"}

    def purge_credentials(self, handle):
        handle["credentials_purged"] = True

    def stop_task(self, handle):
        if self.validation_failure:
            raise RuntimeError(self.validation_failure)
        return {"verified": True}

    def destroy_task(self, handle, *, keep_data=True):
        self.registry[handle["attempt_id"]]["removed"] = not keep_data
        self.release(handle["task_id"])
        return {"verified": True}

    def export_execution(self, handle, execution_id, destination):
        return {"exported": True}

    def task_usage(self, handle):
        return 0

    def collect_retired(self):
        return {"removed": [], "protected": []}

    def leases(self):
        return dict(self._leases)

    def generation(self, ident):
        return self.registry[ident]

    def generations(self):
        return dict(self.registry)

    def mark_dirty(self, ident, task_id):
        self.registry[ident]["dirty"] = True

    def validate_reuse(self, ident):
        if self.validation_failure:
            raise RuntimeError(self.validation_failure)
        self.registry[ident]["dirty"] = False
        return {"status": "pass"}

    def quarantine(self, ident, reason):
        self.quarantined.append((ident, reason))
        return {"stopped": True}

    def release(self, task_id):
        self.released.append(task_id)
        self._leases.pop(task_id, None)


class FakeExecutor:
    """Only the Docker/build boundary is fake; custom tests are actual subprocesses."""
    calls = []
    failures = {}

    def __init__(self, config, state_dir, generation, task, relay, *, manager=None):
        self.config, self.generation, self.task, self.relay = config, generation, task, relay
        self.root = Path(generation["workspace_host"])

    def prepare(self, variant="candidate"):
        path = self.root / variant
        path.parent.mkdir(parents=True, exist_ok=True)
        self.relay.checkout(self.task["tested_sha" if variant == "candidate" else "base_sha"], path)
        return path

    def stop(self, execution_id):
        self.calls.append(("stop", execution_id))

    def stop_task(self):
        return {"verified": True, "remaining": []}

    def stop_codex(self):
        return {"verified": True, "remaining": []}

    def diagnostic_context(self):
        return {variant: {"runtime_origin": "seed", "python_available": True, "python_bin": sys.executable}
                for variant in ("candidate", "base")}

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        self.calls.append((tool_id, variant, parameters))
        artifact = self.root / "artifacts" / execution_id
        artifact.mkdir(parents=True)
        status, code, reason = "pass", 0, "simulated external tool"
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "environment_fingerprint": self.generation["environment_fingerprint"], "artifact_dir": str(artifact),
                  "tested_sha": self.task["tested_sha" if variant == "candidate" else "base_sha"]}
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

    def test_gitee_upload_completes_without_github(self):
        worker = self.worker()
        worker.scan()
        row = worker.journal.task(self.fixture.task["task_id"])
        self.assertEqual(row["phase"], "complete")
        result = json.loads(Path(worker.journal.outbox(row["task_id"])["payload_path"]).read_text())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["required_checks"], list(TOOLS))
        self.assertEqual(result["publication"], {"status": "pending_upload", "completion_requires": ["gitee_upload"]})
        self.assertEqual(json.loads(row["detail"])["completion_boundary"], "gitee_upload")
        self.assertIsNotNone(worker.journal.outbox(row["task_id"])["published"])
        self.assertNotIn("receipt", worker.journal.outbox(row["task_id"]))
        self.assertNotIn("receipts/", git(self.fixture.bare, "ls-tree", "-r", "--name-only", "refs/heads/local-ci-control"))
        with self.assertRaises(ContractError):
            worker.journal.resume(row["task_id"])

    def test_worker_revision_mismatch_is_reported_before_environment(self):
        worker = self.worker()
        with patch("agent_ci.worker.validate_control_revision", side_effect=ContractError("installed revision differs")):
            worker.scan()
        self.assertFalse(self.manager.acquired)
        result = json.loads(Path(worker.journal.outbox(self.fixture.task["task_id"])["payload_path"]).read_text())
        self.assertEqual(result["status"], "infra_error")
        self.assertIn("installed revision differs", result["unfinished"])

    def test_rebuilt_dependency_invalidates_downstream_evidence(self):
        supervisor = self.supervisor()
        for tool in TOOLS[:4]:
            supervisor.poll_check(supervisor.start_check(tool, "Build verified frontend")["execution_id"], 30)
        self.assertTrue(supervisor.fresh("frontend_smoke"))
        supervisor.poll_check(supervisor.start_check("frontend_build", "Rebuild with a changed option", force=True)["execution_id"], 30)
        self.assertFalse(supervisor.fresh("frontend_smoke"))
        with self.assertRaises(ContractError):
            supervisor.start_check("backend_rebuild", "Stale frontend must not be consumed")

    def test_same_recipe_in_new_generation_does_not_reuse_installation(self):
        supervisor = self.supervisor()
        supervisor.poll_check(supervisor.start_check("environment", "Original generation")["execution_id"], 30)
        self.assertTrue(supervisor.fresh("environment"))
        supervisor.executor.generation["generation"] = "replacement-generation"
        self.assertFalse(supervisor.fresh("environment"))

    def test_failed_sealing_cannot_launch_more_code_after_reuse_check(self):
        supervisor = self.supervisor()
        supervisor.poll_check(supervisor.start_check("environment", "Prepare evidence")["execution_id"], 30)
        with patch("agent_ci.supervisor.shutil.copyfile", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                supervisor.finish("Seal attempt")
        with self.assertRaises(ContractError):
            supervisor.start_check("environment", "Mutate checked environment", force=True)
        self.assertEqual("infra_error", supervisor.finish("Retry sealing existing evidence")["status"])

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

    def test_code_failure_delivery_is_complete_but_not_green_or_resumable(self):
        FakeExecutor.failures = {"frontend_build": [("fail", "invalid candidate")]}
        worker = self.worker()
        worker.scan()
        ident = self.fixture.task["task_id"]
        self.assertEqual(worker.journal.task(ident)["phase"], "complete")
        self.assertEqual(worker.journal.result_status(worker.journal.outbox(ident)), "fail")
        with self.assertRaises(ContractError):
            worker.journal.resume(ident)

    def test_uncertain_upload_success_retries_identical_result_without_codex(self):
        worker = self.worker()
        publish = self.fixture.relay.publish_result
        def lost_response(*args):
            publish(*args)
            raise OSError("upload completed but response lost")
        with patch.object(self.fixture.relay, "publish_result", side_effect=lost_response):
            worker.scan()
        ident = self.fixture.task["task_id"]
        box = worker.journal.outbox(ident)
        payload = Path(box["payload_path"]).read_bytes()
        commit = git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-results")
        replacement = self.worker()
        replacement.scan()
        self.assertEqual(replacement.journal.task(ident)["phase"], "complete")
        self.assertEqual(self.driver.calls, 1)
        self.assertEqual(payload, Path(box["payload_path"]).read_bytes())
        self.assertEqual(commit, git(self.fixture.bare, "rev-parse", "refs/heads/local-ci-results"))

    def test_sealed_digest_change_blocks_upload_without_model_recovery(self):
        worker = self.worker()
        worker.process(self.fixture.task)
        ident = self.fixture.task["task_id"]
        box = worker.journal.outbox(ident)
        Path(box["payload_path"]).write_text("changed sealed evidence")
        with patch.object(self.fixture.relay, "publish_result") as publish:
            worker.retry_delivery(worker.journal.task(ident))
            publish.assert_not_called()
        self.assertEqual(worker.journal.task(ident)["phase"], "publishing")
        self.assertIn("digest changed", json.loads(worker.journal.task(ident)["detail"])["upload_error"])
        self.assertEqual(self.driver.calls, 1)

    def test_invalid_identity_and_llvm_snapshot_do_not_execute(self):
        task = {**self.fixture.task, "tested_sha": "b" * 40}
        with self.assertRaises(ContractError):
            validate_task(task)
        task = {**self.fixture.task, "llvm_hash": "b" * 40}
        self.assertFalse(self.fixture.relay.validity(task)[0])

    def test_cancel_does_not_accept_old_result(self):
        worker = self.worker()
        worker.process(self.fixture.task)
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
            result = supervisor.run_custom("reproduce.py", script, "python", "Determine candidate causality", variant, source_only=True, mode="reproduction")
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

    def test_policy_uses_frozen_python_semantics_before_path_risk(self):
        repo = self.fixture.source
        path = repo / "python/triton_anchor/__init__.py"

        def commit(source, message):
            path.write_text(source)
            git(repo, "add", ".")
            git(repo, "commit", "-qm", message)
            return git(repo, "rev-parse", "HEAD")

        previous = commit('__version__ = "0.1"  # type: str\n', "policy base")
        cases = (
            ("comment", '# explanation only\n\n__version__="0.1"  # type: str\n', True),
            ("assignment", '__version__ = "0.2"  # type: str\n', False),
            ("docstring", '"""Runtime docstring."""\n__version__="0.2"  # type: str\n', False),
            ("type-comment", '"""Runtime docstring."""\n__version__="0.2"  # type: object\n', False),
            ("whitespace", '"""Runtime docstring."""\n\n__version__ = "0.2"  # type: object\n', True),
            ("parse-failure", "def broken(:\n", False),
        )
        no_op_policy = None
        for label, source, equivalent in cases:
            current = commit(source, label)
            changes = changed_files(repo, previous, current)
            policy = minimum_checks(changes, backend_enabled=True)
            with self.subTest(label=label):
                self.assertEqual(changes[0].get("semantic") == "python_ast_equivalent", equivalent)
                self.assertEqual(policy["required_checks"], ["environment"] if equivalent else list(TOOLS[:4]))
            if label == "comment":
                no_op_policy = policy
            previous = current
        self.assertEqual(policy["version"], "impact/v5")
        self.assertEqual(no_op_policy["impact"]["classification"], "trusted_python_ast_equivalent")
        self.assertEqual(no_op_policy["recommended_checks"], [])

    def test_policy_cannot_treat_program_or_structural_change_as_docs(self):
        docs = minimum_checks([{"path": "docs/example.md"}], backend_enabled=True)
        self.assertEqual(docs["required_checks"], ["environment"])
        self.assertEqual(docs["impact"]["level"], "non_executable")
        program = minimum_checks([{"path": "scripts/local_ci/skills/local-ci/references/AI_CI_PROGRAM.md"}], backend_enabled=True)
        self.assertIn("contract_tests", program["required_checks"])
        renamed = minimum_checks([{"path": "docs/example.md", "old_path": "csrc/old.cpp", "status": "R100"}], backend_enabled=True)
        self.assertEqual(renamed["required_checks"], list(TOOLS[:6]))
        tests = minimum_checks([{"path": "python/triton_anchor/tests/test_frontend.py", "status": "M"}], backend_enabled=True)
        self.assertEqual(tests["required_checks"], ["environment"])
        self.assertEqual(tests["recommended_checks"], list(TOOLS[1:4]))
        pipeline = minimum_checks([{"path": "python/triton_anchor/pipeline.py", "status": "M"}], backend_enabled=True)
        self.assertEqual(pipeline["required_checks"], list(TOOLS[:6]))
        self.assertEqual(pipeline["recommended_checks"], list(TOOLS[6:]))
        self.assertEqual(minimum_checks([{"path": "unknown.cfg"}], backend_enabled=True)["required_checks"], list(TOOLS))
        self.assertEqual(minimum_checks([{"path": "docs/example.md"}], backend_enabled=True, full=True)["required_checks"], list(TOOLS))
        for status, old_mode, mode, expected in (("A", "000000", "100644", TOOLS[:4]),
                                                  ("D", "100644", "000000", TOOLS[:4]),
                                                  ("M", "100644", "100755", TOOLS)):
            change = {"path": "python/triton_anchor/new.py", "status": status, "old_mode": old_mode, "mode": mode}
            self.assertEqual(minimum_checks([change], backend_enabled=True)["required_checks"], list(expected))
        with self.assertRaises(ContractError):
            minimum_checks([], backend_enabled=True)


if __name__ == "__main__":
    unittest.main()
