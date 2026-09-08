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
        self.generation = {"environment_fingerprint": "fixture-environment", "backend_enabled": False}

    def prepare(self, variant="candidate"):
        path = self.root / variant
        path.mkdir(exist_ok=True)
        (path / "README.md").write_text("fixture source\n")
        return path

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        self.calls.append({"tool_id": tool_id, "custom": custom})
        artifact = self.root / execution_id
        artifact.mkdir()
        code = 0
        record = {"execution_id": execution_id, "tool_id": tool_id, "variant": variant,
                  "environment_fingerprint": self.generation["environment_fingerprint"], "artifact_dir": str(artifact)}
        if custom is not None:
            script = artifact / custom["name"]
            script.write_text(custom["content"])
            command = [sys.executable, "-I", *(["-S"] if custom.get("source_only") else []), str(script)]
            process = subprocess.run(command, capture_output=True, timeout=5)
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
        self.task = {"task_id": "a" * 64}
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

    def test_invalid_types_ranges_and_nested_fields_never_schedule(self):
        base = {"tool_id": "environment", "reason": "fixture"}
        invalid = [
            ("start_check", None), ("start_check", []),
            ("start_check", {"tool_id": "environment"}),
            ("start_check", {**base, "force": "true"}),
            ("start_check", {**base, "variant": "other"}),
            ("start_check", {**base, "reason": "  "}),
            ("start_check", {**base, "parameters": None}),
            ("start_check", {**base, "parameters": {"custom": {}}}),
            ("start_check", {**base, "parameters": {"max_jobs": True}}),
            ("start_check", {**base, "parameters": {"max_jobs": 2.0}}),
            ("start_check", {**base, "parameters": {"max_jobs": 0}}),
            ("start_check", {**base, "parameters": {"timeout_seconds": -1}}),
            ("start_check", {**base, "parameters": {"operators": "add"}}),
            ("start_check", {**base, "parameters": {"operators": []}}),
            ("start_check", {**base, "parameters": {"operators": [{}]}}),
            ("start_check", {**base, "parameters": {"kernels": ["add\n"]}}),
            ("poll_check", {"execution_id": "a" * 32, "wait_seconds": True}),
            ("poll_check", {"execution_id": "a" * 32, "wait_seconds": 31}),
            ("read_file", {"path": "README.md", "start_line": 0}),
            ("read_file", {"path": "README.md", "max_lines": 501}),
            ("read_artifact", {"execution_id": "a" * 32, "offset": -1}),
            ("submit_review", {"kind": "architecture", "status": "pass", "summary": "fixture", "evidence": ["not an object"]}),
            ("context", {"tool_id": "environment"}),
        ]
        for method, arguments in invalid:
            with self.subTest(method=method, arguments=arguments):
                self.assertEqual(-32602, self.mcp(method, arguments)["error"]["code"])
                self.assertIn("error", self.rpc(method, arguments))
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

    def test_runtime_custom_still_requires_smoke_then_executes(self):
        self.await_check(self.rpc("start_check", {"tool_id": "environment", "reason": "fixture"}))
        arguments = {"name": "runtime.py", "content": "print('runtime fixture')", "language": "python", "reason": "runtime fixture"}
        self.assertIn("error", self.rpc("run_custom", arguments))
        self.assertEqual(1, len(self.records()))
        for tool_id in TOOLS[1:4]:
            self.await_check(self.rpc("start_check", {"tool_id": tool_id, "reason": "fixture"}))
        custom = self.await_check(self.rpc("run_custom", arguments))
        self.assertEqual("custom", custom["execution_kind"])
        self.assertFalse(custom["source_only"])
        self.assertTrue(self.supervisor.fresh("frontend_smoke"))

    def test_custom_validation_cannot_be_reintroduced_through_extra_fields(self):
        arguments = {"name": "check.py", "content": "pass", "language": "python", "reason": "fixture", "source_only": True}
        for changed in (
            {**arguments, "tool_id": "environment"},
            {**arguments, "custom": {}},
            {**arguments, "source_only": "true"},
            {**arguments, "name": "../check.py"},
            {**arguments, "content": "x" * (128 * 1024 + 1)},
        ):
            self.assertEqual(-32602, self.mcp("run_custom", changed)["error"]["code"])
            self.assertIn("error", self.rpc("run_custom", changed))
        self.assertEqual([], self.records())

    def test_custom_record_cannot_satisfy_a_builtin_after_restart(self):
        for marker in ({"script_digest": "f" * 64}, {"source_only": True}, {"execution_kind": "custom"}):
            self.journal.execution(self.task["task_id"], "environment", "candidate", {
                "execution_id": uuid.uuid4().hex, "status": "pass", "environment_fingerprint": "fixture-environment", **marker})
            self.assertFalse(self.supervisor.fresh("environment"))


if __name__ == "__main__":
    unittest.main()
