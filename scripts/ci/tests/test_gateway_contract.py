from __future__ import annotations

import copy
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("gateway_v4", ROOT / "scripts/ci/gateway_v4.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()


def body(types="docs"):
    fields = {"types": types, "purpose": "Correct documented behavior", "scope": "README",
              "validation": "Reviewed the actual implementation", "subject": "Public README",
              "consistency": "Names match source", "reproduction": "Run the regression case",
              "expected_actual": "Expected 2, actual 1", "behavior": "New behavior is documented",
              "compatibility": "Existing callers continue to work", "baseline": "Base commit",
              "measurement": "Repeat the same inputs five times", "expected_change": "Lower compile time",
              "versions": "Old and new versions recorded", "sources": "Trusted mirror",
              "environment": "Frontend dependency changes", "recovery": "Restore known-good generation",
              "ci_impact": "Triggers and permissions stay scoped", "coverage": "Regression behavior",
              "execution": "pytest regression case"}
    return "\n".join("<!-- field:" + key + " -->\n" + value for key, value in fields.items())


class FakeGitHub:
    repository = g.REPOSITORY

    def __init__(self, base, head, tested):
        self.base, self.head, self.tested = base, head, tested
        self.pull = {"state": "open", "draft": False, "title": "Document the public behavior",
                     "body": body(), "labels": [{"name": "docs"}],
                     "head": {"sha": head, "ref": "docs-topic", "repo": {"full_name": "anteloper-c/triton-anchor"}},
                     "base": {"ref": "main"}}
        self.statuses, self.comments = [], []
        self.environment = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"login": "maintainer"}}]}]}

    def request(self, path, method="GET", data=None):
        if path == "environments/local-ci-fork-approval":
            return self.environment
        if path == "pulls/7":
            return copy.deepcopy(self.pull)
        if path == "git/ref/pull/7/merge":
            return {"object": {"sha": self.tested}}
        if path == "git/commits/" + self.tested:
            return {"parents": [{"sha": self.base}, {"sha": self.head}]}
        if path == "branches/main":
            return {"commit": {"sha": self.head}}
        raise AssertionError(path)

    optional = request

    def content(self, path, ref):
        assert ref == self.tested
        return b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"

    def status(self, task, state, description, url=""):
        self.statuses.append((task["task_id"], state))

    def comment(self, task, content):
        self.comments.append(content)


class GatewayBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "config", "user.email", "test@example.invalid")
        version = self.source / "triton/python/triton/__init__.py"
        version.parent.mkdir(parents=True)
        version.write_text("__version__ = '3.0.0'\n")
        (self.source / "README.md").write_text("old\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        self.base = git(self.source, "rev-parse", "HEAD")
        (self.source / "README.md").write_text("new\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "head")
        self.head = git(self.source, "rev-parse", "HEAD")
        tree = git(self.source, "rev-parse", "HEAD^{tree}")
        self.tested = subprocess.check_output(["git", "commit-tree", tree, "-p", self.base, "-p", self.head],
                                             cwd=self.source, input=b"merge\n").decode().strip()
        git(self.source, "checkout", "--detach", self.tested)
        self.remote = self.root / "gitee.git"
        git(self.root, "init", "--bare", "-q", str(self.remote))
        self.gh = FakeGitHub(self.base, self.head, self.tested)
        self.task = g.prepare_task(self.gh, self.base, 7)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def store(self, branch):
        result = g.GitStore(str(self.remote), branch)
        self.stores.append(result)
        return result

    def result(self):
        return {"schema": g.RESULT_SCHEMA, "task": self.task, "run_id": "20260907T120000Z-1", "status": "pass",
                "required_checks": ["environment", "contract_tests"],
                "checks": [{"tool_id": key, "status": "pass", "required": True, "reason": "",
                            "execution_id": key + "-1", "exit_code": 0}
                           for key in ("environment", "contract_tests")],
                "reviews": {"pr_info": {"status": "pass", "summary": "clear", "evidence": []},
                            "architecture": {"status": "pass", "summary": "compatible", "evidence": ["README.md"]}},
                "findings": [], "blockers": [], "unfinished": [], "performance": [],
                "environment": {"backend_enabled": True}}

    def test_exact_identity_metadata_and_worker_revision(self):
        self.assertEqual(g.validate_task(self.task), self.task)
        self.assertTrue(self.task["external_fork"])
        for field, value in (("title", "different"), ("worker_revision_sha", "f" * 40), ("full", True)):
            changed = {**self.task, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                g.validate_task(changed)
        self.gh.pull["head"]["sha"] = "e" * 40
        with self.assertRaises(ValueError):
            g.prepare_task(self.gh, self.base, 7, requested_sha=self.head)
        task_file = self.root / "task.json"
        changed = {**self.task, "llvm_hash": "e" * 40}
        task_file.write_bytes(g.canonical(changed))
        with self.assertRaises(ValueError):
            g.load_task(task_file, g.digest(self.task))

    def test_type_specific_information_and_multi_type_union(self):
        for kind in g.TYPE_FIELDS:
            with self.subTest(kind=kind):
                self.assertEqual(g.validate_pr_info({**self.task, "description": body(kind)}), [])
                missing = body(kind).replace("<!-- field:" + g.TYPE_FIELDS[kind][0] + " -->", "<!-- field:unused -->")
                self.assertTrue(g.validate_pr_info({**self.task, "description": missing}))
        self.assertEqual(g.validate_pr_info({**self.task, "description": body("fix, ci")}), [])
        self.assertTrue(g.validate_pr_info({**self.task, "description": body().replace("docs", "- [ ] docs", 1)}))
        self.assertEqual(g.validate_pr_info({**self.task, "description": body().replace("docs", "- [x] docs", 1)}), [])

    def test_external_fork_cannot_use_an_unprotected_environment(self):
        g.validate_approval_environment(self.gh)
        for environment in ({}, {"protection_rules": []}, {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}):
            self.gh.environment = environment
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                g.validate_approval_environment(self.gh)

    def test_real_git_enqueue_manifest_last_and_idempotent_retry(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        self.assertEqual(control.get("tasks/" + self.task["task_id"] + ".json"), self.task)
        self.assertEqual(control.get("current/" + g.current_key(self.task) + ".json")["task_id"], self.task["task_id"])
        for key, ref in (("tested_sha", "task_ref"), ("base_sha", "base_task_ref"), ("head_sha", "head_task_ref")):
            actual = git(self.root, "--git-dir=" + str(self.remote), "rev-parse", "refs/heads/" + self.task[ref])
            self.assertEqual(actual, self.task[key])
        retry = {**self.task, "captured_at": "2099-01-01T00:00:00Z"}
        g.enqueue(retry, self.gh, control, self.source)
        self.assertEqual(control.get("tasks/" + self.task["task_id"] + ".json"), self.task)

    def test_lifecycle_cancellation_reaches_gitee(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        for update in ({"draft": True}, {"state": "closed"}, {"body": body("fix")}):
            self.gh.pull.update(update)
            self.assertFalse(g.is_current(self.gh, self.task))
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 1)
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 0)
        self.assertEqual(control.get("cancel/" + self.task["task_id"] + ".json")["task_id"], self.task["task_id"])

    def test_result_receipt_requires_status_comment_then_dashboard_success(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        name = "runs/v4/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json"
        results.put({name: result})
        receipts = g.collect_results(self.gh, control, results, self.root / "dashboard")
        receipt_name = "receipts/" + self.task["task_id"] + "/" + result["run_id"] + ".json"
        self.assertIsNone(control.get(receipt_name))
        self.assertEqual(receipts[0]["result_digest"], hashlib.sha256((results.root / name).read_bytes()).hexdigest())
        self.assertEqual(self.gh.statuses[-1][1], "success")
        self.assertIn("Local CI", self.gh.comments[-1])
        g.acknowledge(self.gh, control, results, receipts)
        self.assertEqual(control.get(receipt_name)["status"], "complete")
        again = g.collect_results(self.gh, control, results, self.root / "dashboard")
        self.assertEqual(again, [])
        self.gh.pull["draft"] = True
        self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])

    def test_comment_failure_does_not_acknowledge(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put({"runs/v4/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json": result})
        with patch.object(self.gh, "comment", side_effect=RuntimeError("comment unavailable")):
            self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])
        snapshot = json.loads((self.root / "dashboard/v4-tasks.json").read_text())
        self.assertEqual(snapshot["tasks"][0]["status"], "infra_error")
        self.assertFalse((control.root / "receipts").exists())

    def test_infrastructure_failure_can_report_but_cancelled_receipt_cannot_complete(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        result.update(status="infra_error", checks=[], required_checks=[], reviews={}, unfinished=["environment failed"])
        results.put({"runs/v4/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json": result})
        with patch.object(g, "trusted_minimum", side_effect=AssertionError("broken version need not be read for a failing result")):
            receipts = g.collect_results(self.gh, control, results, self.root / "dashboard")
        self.assertEqual(len(receipts), 1)
        with self.assertRaises(ValueError):
            g.acknowledge(self.gh, control, results, [{**receipts[0], "tested_sha": "e" * 40}])
        control.put({"cancel/" + self.task["task_id"] + ".json": {"task_id": self.task["task_id"], "reason": "cancel after collection"}})
        g.acknowledge(self.gh, control, results, receipts)
        self.assertIsNone(control.get("receipts/" + self.task["task_id"] + "/" + result["run_id"] + ".json"))

    def test_invalid_results_cannot_claim_success(self):
        for mutate in (
            lambda r: r.update(required_checks=[]),
            lambda r: r["checks"][0].update(status="not_applicable", reason="AI chose to skip"),
            lambda r: r["reviews"]["architecture"].update(evidence=[]),
            lambda r: r.update(blockers=["deterministic regression"]),
            lambda r: r.update(run_id="../../escape"),
            lambda r: r["task"].update(worker_revision_sha="e" * 40),
        ):
            result = copy.deepcopy(self.result())
            mutate(result)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                g.validate_result(result, self.task)

    def test_optimistic_control_writes_preserve_other_writer(self):
        first, second = self.store(g.CONTROL_BRANCH), self.store(g.CONTROL_BRANCH)
        first.put({"a.json": {"a": 1}})
        second.put({"b.json": {"b": 2}})
        first.refresh()
        self.assertEqual(first.get("a.json"), {"a": 1})
        self.assertEqual(first.get("b.json"), {"b": 2})
        with self.assertRaises(ValueError):
            first.put({"a.json": {"a": 3}}, ("a.json",))

    def test_security_scans_real_git_diff(self):
        (self.source / "unsafe.py").write_text("import socket\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "unsafe")
        tested = git(self.source, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as output:
            original = Path.cwd()
            try:
                import os
                os.chdir(output)
                self.assertEqual(g.security_diff(self.source, self.base, tested), 1)
            finally:
                os.chdir(original)

    def test_sarif_severity_gate_executes(self):
        folder = self.root / "sarif"
        folder.mkdir()
        document = {"runs": [{"tool": {"driver": {"rules": [{"id": "r", "properties": {"security-severity": "7.5"}}]}},
                              "results": [{"ruleId": "r", "level": "warning"}]}]}
        (folder / "result.sarif").write_text(json.dumps(document))
        self.assertEqual(len(g.sarif_failures(folder)), 1)
        document["runs"][0]["results"] = []
        (folder / "result.sarif").write_text(json.dumps(document))
        self.assertEqual(g.sarif_failures(folder), [])

    def test_production_transport_allowlists(self):
        with self.assertRaises(ValueError):
            g.GitHub("RACE-org/triton-anchor")
        with self.assertRaises(ValueError):
            g.GitStore("https://github.com/RACE-org/triton-anchor", g.CONTROL_BRANCH)

    def test_http_status_and_idempotent_comment_writeback(self):
        calls, comments = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(comments).encode())

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("POST", self.path, data))
                if self.path.endswith("/comments"):
                    comments.append({"id": 11, "user": {"type": "Bot"}, **data})
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_PATCH(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("PATCH", self.path, data))
                comments[0].update(data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = g.GitHub(g.REPOSITORY, f"http://127.0.0.1:{server.server_port}", token="fake-local-token")
            client.status(self.task, "pending", "Queued")
            client.comment(self.task, "first report")
            client.comment(self.task, "first report")
            client.comment(self.task, "updated report")
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[-1][0], "PATCH")
            self.assertTrue(comments[0]["body"].startswith(g.MARKER))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class WorkflowStructureTests(unittest.TestCase):
    def test_reusable_dag_and_router_contract(self):
        import yaml
        worker_text = (ROOT / ".github/workflows/ci-gateway.yml").read_text()
        data = yaml.load(worker_text, Loader=yaml.BaseLoader)
        jobs = data["jobs"]
        self.assertEqual(jobs["basic"]["needs"], "prepare")
        self.assertIn("basic", jobs["api"]["needs"])
        self.assertIn("api", jobs["security"]["needs"])
        self.assertIn("security", jobs["review-card"]["needs"])
        self.assertIn("review-card", jobs["approve-external-fork"]["needs"])
        self.assertIn("approve-external-fork", jobs["enqueue"]["needs"])
        for name in ("ci_basic.yml", "api-compat.yml", "security-gate.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(set(workflow["on"]), {"workflow_call"})
        router = ROOT.parent / "triton-anchor-main/.github/workflows/ci-gateway.yml"
        if router.exists():
            self.assertEqual(router.read_text().rstrip(), worker_text.split("\n  cancel-obsolete:", 1)[0].rstrip())


if __name__ == "__main__":
    unittest.main()
