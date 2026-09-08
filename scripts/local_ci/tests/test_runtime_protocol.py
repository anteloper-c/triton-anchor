"""Behavioral gate and real local Git transport tests; no external service calls."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.common import digest, git, read_json, write_json  # noqa: E402
from runtime.policy import BACKEND_TOOLS, minimum_checks, validate_task  # noqa: E402
from runtime.relay import Relay  # noqa: E402
from runtime.report import build_result  # noqa: E402


def admitted_task():
    task = {"schema": "triton-anchor-local-ci-task-metadata/v2", "repository": "anteloper-c/triton-anchor",
            "task_id": "task-test", "task_ref": "ci/pr-7/triton-3.2", "event_kind": "pull_request",
            "pr_number": 7, "target_branch": "main", "tested_sha": "a" * 40, "target_sha": "a" * 40,
            "head_sha": "b" * 40, "base_sha": "c" * 40, "worker_revision_sha": "d" * 40,
            "head_repo": "contributor/triton-anchor", "captured_at": "2026-09-08T10:00:00Z",
            "title": "Fix compiler behavior", "description": "Fix the compiler behavior for a masked scalar load.",
            "preflight": dict.fromkeys(("pr_information", "basic", "api", "security"), "success")}
    task["approval"] = {"required": True, "status": "approved",
                        **{key: task[key] for key in ("head_sha", "base_sha", "tested_sha", "worker_revision_sha")}}
    return task


class PolicyAndReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        (self.source / "README.md").write_text("Architecture contract fixture\n")
        self.task = admitted_task()
        self.policy = minimum_checks(["python/triton_anchor/pipeline.py"], {"triton_version": "3.2"})
        self.broker = SimpleNamespace(
            context={"source_host_dir": str(self.source)}, performance=[],
            receipts=[{"id": "receipt-" + tool, "tool": tool, "returncode": 0, "termination": None}
                      for tool in self.policy["required"] if tool != "architecture_review"],
            checks={tool: {"id": tool, "status": "passed", "reason": "host command completed", "evidence": ["receipt-" + tool]}
                    for tool in self.policy["required"] if tool != "architecture_review"},
            review={"summary": "已检查编译器变更。", "pr_information": {"status": "passed", "summary": "意图与变更一致"}, "architecture": {"status": "passed", "summary": "契约保持一致。",
                    "evidence": [{"path": "README.md", "reason": "核对前端与后端边界", "sha256": digest(self.source / "README.md")}]},
                    "findings": [], "performance": []})

    def result(self, **kwargs):
        return build_result(self.task, "run-test", self.policy, self.broker,
                            started_at="2026-09-08T10:01:00Z", source_unchanged=True, **kwargs)

    def test_missing_required_frontend_check_cannot_pass(self):
        self.broker.checks.pop("wheel_install")
        result = self.result()
        self.assertNotEqual(result["conclusion"], "success")
        self.assertTrue(any("wheel_install" in reason for reason in result["blocking_reasons"]))

    def test_missing_or_failed_pr_intent_review_cannot_pass(self):
        self.broker.review.pop('pr_information')
        self.assertEqual(self.result()['conclusion'], 'error')
        self.broker.review['pr_information'] = {'status': 'failed', 'summary': '变更与声明意图不一致'}
        self.assertEqual(self.result()['conclusion'], 'failure')

    def test_architecture_evidence_must_identify_a_real_source_file(self):
        for bad_path in ("missing_contract.md", "../outside.md", "/etc/passwd"):
            with self.subTest(path=bad_path):
                self.broker.review["architecture"]["evidence"] = [{"path": bad_path, "reason": "claimed review"}]
                self.assertNotEqual(self.result()["conclusion"], "success")

    def test_missing_architecture_review_cannot_pass(self):
        self.broker.review["architecture"]["evidence"] = []
        self.assertEqual(self.result()["conclusion"], "error")

    def test_pass_flag_without_corresponding_host_receipt_cannot_pass(self):
        self.broker.receipts = []
        self.assertNotEqual(self.result()["conclusion"], "success")

    def test_timeout_is_not_deterministic_code_failure(self):
        self.broker.receipts.append({"id": "command-1", "returncode": -9, "termination": "timeout"})
        finding = {"summary": "possible failure", "blocking": True, "severity": "high", "caused_by_change": True,
                   "code_evidence": [{"path": "README.md", "line": 1}], "reproduction_receipts": ["command-1"]}
        self.broker.review["findings"] = [finding]
        result = self.result()
        self.assertFalse(result["ai_review"]["findings"][0]["blocking"])
        self.assertNotIn("possible failure", result["blocking_reasons"])

    def test_pure_performance_regression_does_not_block(self):
        self.broker.performance = [{"tool": "compile_time", "change_ratio": 1.0, "status": "warning"}]
        self.assertEqual(self.result()["conclusion"], "success")

    def test_docs_allowlist_does_not_exempt_executable_control_plane(self):
        docs = minimum_checks(["docs/design.md"], {"triton_version": "3.0"})
        self.assertTrue(docs["docs_only"])
        self.assertNotIn("frontend_build", docs["required"])
        control = minimum_checks([".github/workflows/example.md"], {"triton_version": "3.0"})
        self.assertFalse(control["docs_only"])
        self.assertIn("control_plane", control["required"])
        unknown = minimum_checks([".gitmodules"], {"triton_version": "3.0"})
        self.assertTrue(set(BACKEND_TOOLS) <= set(unknown["required"]))

    def test_external_approval_cannot_be_reused_for_another_commit(self):
        validate_task(self.task)
        self.task["approval"]["base_sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_task(self.task)


class RealGitRelayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.seed)], check=True, capture_output=True)
        git(self.seed, "config", "user.name", "CI protocol fixture")
        git(self.seed, "config", "user.email", "fixture@invalid")
        git(self.seed, "config", "core.autocrlf", "false")
        (self.seed / "README.md").write_text("Actual Git source fixture\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "source fixture")
        self.sha = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        self.task = admitted_task()
        self.task.update(target_sha=self.sha, tested_sha=self.sha)
        self.task["approval"]["tested_sha"] = self.sha
        git(self.seed, "push", "origin", f"{self.sha}:refs/heads/{self.task['task_ref']}")
        self.push_document(self.task["task_ref"].replace("ci/", "ci/meta/", 1), "task-metadata.json", self.task)
        self.relay = Relay({"url": str(self.remote)}, self.root / "state")

    def push_document(self, ref, name, document):
        write_json(self.seed / name, document)
        git(self.seed, "add", name)
        if git(self.seed, "diff", "--cached", "--quiet", check=False).returncode:
            git(self.seed, "commit", "-m", "protocol fixture document")
        git(self.seed, "push", "origin", f"HEAD:refs/heads/{ref}")

    def output(self, run_id="run-one"):
        result = {"schema": "triton-anchor-local-ci-result/v4", "task_id": self.task["task_id"],
                  "run_id": run_id, "conclusion": "success", "evidence": [], "tested_sha": self.sha}
        out = self.root / run_id
        out.mkdir()
        write_json(out / "result.json", result)
        (out / "report.md").write_text("Local CI protocol fixture report\n")
        return out, result

    def test_current_task_and_git_checkout_preserve_exact_commit(self):
        self.assertTrue(self.relay.current(self.task))
        destination = self.root / "checkout"
        snapshot = self.relay.checkout(self.sha, destination)
        self.assertEqual(git(destination, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(snapshot["README.md"], digest(destination / "README.md"))
        self.assertFalse(any(name.startswith(".git/") for name in snapshot))

    def test_old_cancellation_cannot_cancel_a_newly_dispatched_task(self):
        cancellation = {"repository": self.task["repository"], "head_sha": self.task["head_sha"],
                        "task_ref": self.task["task_ref"], "target_branch": self.task["target_branch"],
                        "cancel_all": True, "cancelled_at": "2026-09-08T09:00:00Z"}
        self.push_document(self.task["task_ref"].replace("ci/", "ci/cancel/", 1), "cancellation.json", cancellation)
        self.assertTrue(self.relay.current(self.task))
        cancellation["cancelled_at"] = "2026-09-08T11:00:00Z"
        self.push_document(self.task["task_ref"].replace("ci/", "ci/cancel/", 1), "cancellation.json", cancellation)
        self.assertFalse(self.relay.current(self.task))

    def test_metadata_identity_change_invalidates_current_task(self):
        changed = copy.deepcopy(self.task)
        changed["base_sha"] = "f" * 40
        self.push_document(self.task["task_ref"].replace("ci/", "ci/meta/", 1), "task-metadata.json", changed)
        self.assertFalse(self.relay.current(self.task))

    def test_published_artifact_hashes_match_real_git_content(self):
        out, result = self.output()
        relative = self.relay.publish(out, result)
        raw = git(self.remote, "show", f"local-ci-results:{relative}/publish-manifest.json")
        manifest = json.loads(raw)
        self.assertTrue(manifest["files"])
        for item in manifest["files"]:
            self.assertEqual(item["sha256"], digest(out / item["path"]))
        before = git(self.remote, "rev-parse", "refs/heads/local-ci-results")
        self.relay.publish(out, result)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/local-ci-results"), before)

    def test_same_run_cannot_rewrite_a_published_report(self):
        out, result = self.output()
        self.relay.publish(out, result)
        (out / "report.md").write_text("Changed evidence after publication\n")
        with self.assertRaisesRegex(ValueError, "immutable|different|hash"):
            self.relay.publish(out, result)

    def test_log_changed_after_receipt_cannot_be_published(self):
        out, result = self.output()
        (out / "logs").mkdir()
        log = out / "logs" / "command-0001.log"
        log.write_text("original command output\n")
        result["evidence"] = [{"id": "command-0001", "log_path": "logs/command-0001.log", "log_sha256": digest(log)}]
        write_json(out / "result.json", result)
        write_json(out / "evidence.json", result["evidence"])
        log.write_text("rewritten output\n")
        with self.assertRaisesRegex(ValueError, "hash|digest|evidence"):
            self.relay.publish(out, result)

    def test_real_push_rejection_retries_without_reexecuting_task(self):
        initial, seed_result = self.output("seed-run")
        self.relay.publish(initial, seed_result)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\nif test ! -e allow-next-push; then\n  : > allow-next-push\n  echo "intentional transient rejection" >&2\n  exit 1\nfi\nexit 0\n', newline="\n")
        hook.chmod(0o755)
        out, result = self.output("retried-run")
        before = digest(out / "result.json")
        relative = self.relay.publish(out, result)
        self.assertTrue((self.remote / "allow-next-push").exists())
        self.assertEqual(digest(out / "result.json"), before)
        published = json.loads(git(self.remote, "show", f"local-ci-results:{relative}/result.json"))
        self.assertEqual(published, result)


if __name__ == "__main__":
    unittest.main()
