"""Public MCP/RPC validation with real sockets, journal and custom subprocesses."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci import mcp_server
from agent_ci.executor import DockerExecutor
from agent_ci.policy import TOOLS
from agent_ci.protocol import ContractError
from agent_ci.state import Journal
from agent_ci.supervisor import Supervisor, ToolService


class BoundaryExecutor:
    """Build execution is a fixture; legal custom Python really executes locally."""
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.config = {"max_jobs": 8, "tool_timeouts": {"environment": 60}}
        self.generation = {"generation": "fixture-generation", "environment_fingerprint": "fixture-environment", "backend_enabled": False}

    def prepare(self, variant="candidate"):
        path = self.root / variant
        path.mkdir(exist_ok=True)
        (path / "README.md").write_text("fixture source\n")
        return path

    def diagnostic_context(self):
        return {variant: {"runtime_origin": "seed", "python_available": True} for variant in ("candidate", "base")}

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        self.calls.append({"tool_id": tool_id, "custom": custom})
        artifact = self.root / execution_id
        artifact.mkdir()
        code = 0
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "tested_sha": ("b" if variant == "base" else "c") * 40,
                  "environment_fingerprint": self.generation["environment_fingerprint"], "artifact_dir": str(artifact)}
        if custom is not None:
            script = artifact / custom["name"]
            script.write_text(custom["content"])
            command = [sys.executable, "-I", *(["-S"] if custom.get("source_only") else []), str(script)]
            process = subprocess.run(command, capture_output=True, timeout=5, env={**os.environ, "FIXTURE_VARIANT": variant})
            code = process.returncode
            (artifact / "execution.log").write_bytes(process.stdout + process.stderr)
            record["script_digest"] = hashlib.sha256(custom["content"].encode()).hexdigest()
        else:
            (artifact / "execution.log").write_text("fixture built-in invocation\n")
        return {**record, "status": "pass" if code == 0 else "fail", "exit_code": code}

    def stop(self, execution_id):
        raise AssertionError("No cancellation expected in this fixture")


@unittest.skipUnless(sys.platform.startswith("linux"), "Unix socket integration")
class MCPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-mcp-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / "state")
        self.executor = BoundaryExecutor(self.root)
        self.task = {"task_id": "a" * 64, "tested_sha": "c" * 40, "base_sha": "b" * 40}
        self.policy = {"capabilities": [*TOOLS[:4], "contract_tests"], "required_checks": list(TOOLS[:4])}
        self.supervisor = Supervisor(self.task, self.policy, self.journal, self.executor, self.root / "run")
        self.addCleanup(self.supervisor.close)
        self.service = ToolService(self.supervisor, self.root / "rpc.sock")
        self.service.__enter__()
        self.addCleanup(self.service.__exit__, None, None, None)

    def records(self):
        return self.journal.executions(self.task["task_id"])

    def rpc(self, method, arguments, **extra):
        request = {"token": self.service.token, "method": method, "arguments": arguments, **extra}
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(self.service.path))
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                return json.loads(stream.readline())

    def mcp(self, method, arguments):
        with mock.patch.dict(os.environ, {"LOCAL_CI_RPC_SOCKET": str(self.service.path), "LOCAL_CI_RPC_TOKEN": self.service.token}):
            return mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": method, "arguments": arguments}})

    def await_check(self, queued):
        self.assertIn("result", queued, queued)
        ident = queued["result"]["execution_id"]
        polled = self.rpc("poll_check", {"execution_id": ident, "wait_seconds": 5})
        self.assertEqual("pass", polled["result"]["status"], polled)
        return polled["result"]

    def test_mcp_rejects_internal_custom_before_bridge_or_journal(self):
        arguments = {"tool_id": "environment", "reason": "fixture", "custom": {
            "name": "check.py", "content": "pass", "language": "python", "source_only": True}}
        with mock.patch.object(mcp_server, "call") as bridge:
            result = self.mcp("start_check", arguments)
        self.assertEqual(-32602, result["error"]["code"])
        self.assertIn("custom", result["error"]["message"])
        bridge.assert_not_called()
        self.assertEqual([], self.records())
        self.assertEqual([], self.executor.calls)

    def test_direct_rpc_cannot_bypass_schema_or_use_private_queue(self):
        arguments = {"tool_id": "environment", "reason": "fixture", "custom": {
            "name": "check.py", "content": "pass", "language": "python", "source_only": True}}
        self.assertIn("error", self.rpc("start_check", arguments))
        self.assertIn("error", self.rpc("_start_check", arguments))
        self.assertIn("error", self.rpc("context", {}, custom={}))
        self.assertIn("error", self.rpc("context", {}, token="wrong-task-capability"))
        self.assertEqual([], self.records())
        self.assertEqual([], self.executor.calls)

    def test_upload_retry_is_not_an_agent_capability(self):
        self.assertNotIn("retry_publication", mcp_server.SCHEMAS)
        self.assertNotIn("retry_publication", ToolService.METHODS)
        self.assertIn("error", self.rpc("retry_publication", {"reason": "Try to upload from the agent"}))
        self.assertEqual([], self.records())
        self.assertEqual([], self.executor.calls)

    def test_capability_and_trusted_budgets_reject_before_queue(self):
        for arguments in (
            {"tool_id": "custom_1234", "reason": "fixture"},
            {"tool_id": "backend_rebuild", "reason": "fixture"},
            {"tool_id": "environment", "reason": "fixture", "parameters": {"max_jobs": 9}},
            {"tool_id": "environment", "reason": "fixture", "parameters": {"timeout_seconds": 61}},
        ):
            self.assertIn("error", self.rpc("start_check", arguments))
        self.assertEqual([], self.records())
        self.assertEqual([], self.executor.calls)

    def test_public_python_entry_and_executor_refuse_builtin_custom(self):
        self.assertNotIn("custom", inspect.signature(self.supervisor.start_check).parameters)
        custom = {"name": "check.py", "content": "pass", "language": "python", "source_only": True}
        with self.assertRaises(TypeError):
            self.supervisor.start_check("environment", "fixture", custom=custom)
        with self.assertRaisesRegex(ContractError, "built-in"):
            self.supervisor._start_check("environment", "fixture", custom=custom)
        # The executor rejects this mismatch before constructing any artifact or process.
        executor = object.__new__(DockerExecutor)
        with self.assertRaisesRegex(ContractError, "built-in"):
            executor.run("environment", uuid.uuid4().hex, "candidate", {}, threading.Event(), custom)
        self.assertEqual([], self.records())

    def test_legal_mcp_builtin_and_source_only_custom_keep_distinct_identity(self):
        reply = self.mcp("start_check", {"tool_id": "environment", "reason": "validate environment"})
        self.assertFalse(reply["result"]["isError"])
        queued = json.loads(reply["result"]["content"][0]["text"])
        builtin = self.await_check({"result": queued})
        self.assertEqual("builtin", builtin["execution_kind"])
        self.assertIsNone(self.executor.calls[0]["custom"])
        custom = self.await_check(self.rpc("run_custom", {"name": "source.py", "content": "print('fixture custom executed')",
            "language": "python", "reason": "source-only fixture", "source_only": True}))
        self.assertTrue(custom["tool_id"].startswith("custom_"))
        self.assertEqual("custom", custom["execution_kind"])
        self.assertTrue(custom["source_only"])
        self.assertFalse(custom["required"])
        self.assertTrue(self.supervisor.fresh("environment"))
        self.assertFalse(self.supervisor.fresh("frontend_build"))
        log = self.rpc("read_artifact", {"execution_id": custom["execution_id"]})
        self.assertIn("fixture custom executed", log["result"]["content"])

    def test_diagnostic_and_experiment_work_before_environment_and_report_seed(self):
        context = self.rpc("context", {})["result"]
        self.assertEqual("seed", context["diagnostics"]["candidate"]["runtime_origin"])
        self.assertFalse(context["diagnostics"]["candidate"]["strict_reproduction_ready"])
        self.assertEqual([], context["diagnostics"]["candidate"]["diagnostic_requires"])
        arguments = {"name": "early.py", "content": "print('diagnose failed build')", "language": "python", "reason": "inspect before build"}
        for mode in (None, "experiment"):
            with self.subTest(mode=mode):
                request = dict(arguments)
                if mode:
                    request.update(mode=mode)
                result = self.await_check(self.rpc("run_custom", request))
                self.assertEqual(mode or "diagnostic", result["custom_mode"])
                self.assertEqual({}, result["dependency_executions"])
                self.assertFalse(result["required"])
        self.assertFalse(self.supervisor.fresh("environment"))
        self.assertFalse(any(call["custom"] is None for call in self.executor.calls))

    def test_only_strict_reproduction_can_establish_original_sha_causality(self):
        self.supervisor.changes = [{"path": "README.md"}]
        for variant in ("candidate", "base"):
            self.await_check(self.rpc("start_check", {"tool_id": "environment", "reason": "verify source context", "variant": variant}))
        arguments = {"name": "reproduce.py", "content": "import os\nraise SystemExit(os.environ['FIXTURE_VARIANT'] == 'candidate')\n",
                     "language": "python", "reason": "compare original variants", "source_only": True}
        finding = {"path": "README.md", "line": 1, "severity": "high", "summary": "Fixture branch difference"}
        for mode in ("diagnostic", "experiment", "reproduction"):
            ids = []
            for variant in ("candidate", "candidate", "base"):
                queued = self.rpc("run_custom", {**arguments, "mode": mode, "variant": variant})["result"]
                record = self.rpc("poll_check", {"execution_id": queued["execution_id"], "wait_seconds": 5})["result"]
                self.assertEqual("pass" if variant == "base" else "fail", record["status"])
                ids.append(record["execution_id"])
            self.assertEqual(mode == "reproduction", self.supervisor.finding_blocker({**finding, "execution_ids": ids}), mode)
        candidate = next(r for r in self.records() if r["execution_id"] == ids[0])
        candidate["tested_sha"] = "d" * 40
        self.journal.execution(self.task["task_id"], candidate["tool_id"], "candidate", candidate)
        self.assertFalse(self.supervisor.finding_blocker({**finding, "execution_ids": ids}))


if __name__ == "__main__":
    unittest.main()
