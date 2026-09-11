from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "gateway_v4", ROOT / "scripts/ci/gateway_v4.py"
)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def git(root, *args):
    return (
        subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def body(
    summary="Correct documented behavior",
    scope="README",
    validation="Reviewed the actual implementation",
):
    fields = {"summary": summary, "scope": scope, "validation": validation}
    return "\n".join(
        "<!-- field:" + key + " -->\n" + value for key, value in fields.items()
    )


class FakeGitHub:
    repository = g.REPOSITORY

    def __init__(self, base, head, tested):
        self.base, self.head, self.tested = base, head, tested
        self.pull = {
            "state": "open",
            "draft": False,
            "title": "Document the public behavior",
            "body": body(),
            "labels": [{"name": "docs"}],
            "head": {
                "sha": head,
                "ref": "docs-topic",
                "repo": {"full_name": "anteloper-c/triton-anchor"},
            },
            "base": {"ref": "main", "sha": base},
            "mergeable": True,
            "merge_commit_sha": tested,
        }
        self.statuses, self.comments = [], []
        self.latest_statuses, self.writes = {}, []
        self.environment = {
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [
                        {"type": "User", "reviewer": {"login": "maintainer"}}
                    ],
                }
            ]
        }

    def request(self, path, method="GET", data=None):
        if path == "environments/local-ci-fork-approval":
            return self.environment
        if path == "pulls/7":
            return copy.deepcopy(self.pull)
        if path == "git/commits/" + self.tested:
            return {"parents": [{"sha": self.base}, {"sha": self.head}]}
        if path == "branches/main":
            return {"commit": {"sha": self.head}}
        raise AssertionError(path)

    optional = request

    def gitlinks(self, ref):
        return []

    def content(self, path, ref):
        assert ref == self.tested
        return b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"

    def status(self, task, state, description, url=""):
        self.statuses.append((task["task_id"], state))
        self.latest_statuses[task["task_id"]] = (state, description)
        self.writes.append("status")

    def status_matches(self, task, state, description):
        return self.latest_statuses.get(task["task_id"]) == (state, description)

    def check(self, *_args, **_kwargs):
        return False

    def comment(self, task, content):
        if self.comments and self.comments[-1] == content:
            return False
        self.comments.append(content)
        self.writes.append("comment")
        return True


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
        self.tested = (
            subprocess.check_output(
                ["git", "commit-tree", tree, "-p", self.base, "-p", self.head],
                cwd=self.source,
                input=b"merge\n",
            )
            .decode()
            .strip()
        )
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
        if branch == g.RESULTS_BRANCH:
            original_put = result.put

            def put_with_delivery(documents, immutable=()):
                documents = dict(documents)
                for path, document in list(documents.items()):
                    if path.endswith("/result.json"):
                        summary = self.execution_summary()
                        documents[
                            path.replace("result.json", "execution-summary.json")
                        ] = summary
                        raw_digest = hashlib.sha256(
                            g.canonical(document) + b"\n"
                        ).hexdigest()
                        artifact = document.get("artifacts", [{}])[0]
                        documents[
                            path.replace("result.json", "delivery-index.json")
                        ] = {
                            "schema": "triton-anchor-delivery/v1",
                            "task_id": self.task["task_id"],
                            "run_id": document["run_id"],
                            "result_digest": raw_digest,
                            "status": "ready",
                            "artifacts": [
                                {
                                    **artifact,
                                    "status": "ready",
                                    "verified_sha256": artifact.get("sha256"),
                                    "attachment_id": 1,
                                    "release_id": 1,
                                }
                            ],
                        }
                return original_put(documents, immutable)

            result.put = put_with_delivery
        return result

    def execution_summary(self):
        return {
            "schema": "triton-anchor-executions/v1",
            "task_id": self.task["task_id"],
            "run_id": "20260907T120000Z-1",
            "executions": [
                {
                    "tool_id": "control_plane",
                    "execution_id": "environment-1",
                    "status": "pass",
                    "exit_code": 0,
                    "artifact_ids": ["log-1"],
                    "variant": "candidate",
                    "tested_sha": self.task["tested_sha"],
                    "environment_fingerprint": "e" * 64,
                    "original_subject": True,
                }
            ],
        }

    def result(self):
        return {
            "schema": g.RESULT_SCHEMA,
            "task": self.task,
            "run_id": "20260907T120000Z-1",
            "status": "pass",
            "required_checks": ["control_plane"],
            "checks": [
                {
                    "tool_id": "control_plane",
                    "status": "pass",
                    "required": True,
                    "reason": "",
                    "execution_id": "environment-1",
                    "exit_code": 0,
                },
                {
                    "tool_id": "frontend_build",
                    "status": "not_selected",
                    "required": False,
                    "reason": "Trusted frozen diff has no executable semantic change",
                },
            ],
            "reviews": {
                "pr_info": {"status": "pass", "summary": "clear", "evidence": []},
                "architecture": {
                    "status": "pass",
                    "summary": "compatible",
                    "evidence": ["README.md"],
                },
            },
            "findings": [],
            "blockers": [],
            "unfinished": [],
            "performance": [],
            "environment": {
                "backend_enabled": True,
                "environment_fingerprint": "e" * 64,
            },
            "artifacts": [
                {
                    "artifact_id": "log-1",
                    "execution_id": "environment-1",
                    "required": True,
                    "path": "artifacts/log.gz",
                    "sha256": "a" * 64,
                    "size": 100,
                }
            ],
            "execution_summary_sha256": hashlib.sha256(
                g.canonical(self.execution_summary()) + b"\n"
            ).hexdigest(),
        }

    def test_exact_identity_metadata_and_worker_revision(self):
        self.assertEqual(g.validate_task(self.task), self.task)
        self.assertTrue(self.task["external_fork"])
        for field, value in (
            ("title", "different"),
            ("worker_revision_sha", "f" * 40),
            ("full", True),
        ):
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

    def test_prepare_uses_documented_pr_merge_result_and_checks_its_identity(self):
        self.assertEqual(self.task["tested_sha"], self.gh.pull["merge_commit_sha"])
        self.gh.pull["mergeable"] = False
        with self.assertRaisesRegex(ValueError, "cannot be merged cleanly"):
            g.prepare_task(self.gh, self.base, 7)

        self.gh.pull["mergeable"] = True
        self.gh.pull["merge_commit_sha"] = None
        with self.assertRaisesRegex(ValueError, "merge result is not ready"):
            g.prepare_task(self.gh, self.base, 7)
        self.gh.pull["merge_commit_sha"] = self.tested
        self.gh.pull["base"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "Merge parents do not match"):
            g.prepare_task(self.gh, self.base, 7)

    def test_submodules_require_explicit_gitee_mirrors_and_task_specific_refs(self):
        links = [{"path": "FlagGems", "sha": "c" * 40}]
        with patch.object(self.gh, "gitlinks", return_value=links):
            with patch.dict(g.os.environ, {"GITEE_SUBMODULE_MIRRORS": "{}"}):
                with self.assertRaisesRegex(ValueError, "Gitee submodule mirror"):
                    g.prepare_task(self.gh, self.base, 7)
            with patch.dict(
                g.os.environ,
                {
                    "GITEE_SUBMODULE_MIRRORS": '{"FlagGems":"https://gitee.com/test/FlagGems.git"}'
                },
            ):
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(
                    {row["variant"] for row in task["submodules"]},
                    {"candidate", "base"},
                )
                self.assertTrue(
                    all(
                        task["task_id"] in row["task_ref"] for row in task["submodules"]
                    )
                )

    def test_three_required_pr_sections_without_type_specific_fields(self):
        self.assertEqual(g.validate_pr_info(self.task), [])
        heading_body = """## 变更概述 / Summary
Document the behavior
## 影响范围 / Scope
README only
## 验证情况 / Validation
未运行：纯文档变更
"""
        self.assertEqual(
            g.validate_pr_info({**self.task, "description": heading_body}), []
        )
        for field in g.FIELD_NAMES:
            missing = body().replace(f"<!-- field:{field} -->", "<!-- field:unused -->")
            with self.subTest(field=field):
                self.assertTrue(
                    g.validate_pr_info({**self.task, "description": missing})
                )
        for placeholder in ("TODO", "TBD", "待填写", "..."):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(
                    g.validate_pr_info(
                        {**self.task, "description": body(summary=placeholder)}
                    )
                )
        for title in ("WIP", "todo", "TBD"):
            with self.subTest(title=title):
                self.assertTrue(g.validate_pr_info({**self.task, "title": title}))
        for title in ("test", "Update", "更新"):
            with self.subTest(title=title):
                self.assertEqual(g.validate_pr_info({**self.task, "title": title}), [])

    def test_external_fork_cannot_use_an_unprotected_environment(self):
        g.validate_approval_environment(self.gh)
        for environment in (
            {},
            {"protection_rules": []},
            {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]},
        ):
            self.gh.environment = environment
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                g.validate_approval_environment(self.gh)

    def test_real_git_enqueue_manifest_last_and_idempotent_retry(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        self.assertEqual(
            control.get("tasks/" + self.task["task_id"] + ".json"), self.task
        )
        self.assertEqual(
            control.get("current/" + g.current_key(self.task) + ".json")["task_id"],
            self.task["task_id"],
        )
        for key, ref in (
            ("tested_sha", "task_ref"),
            ("base_sha", "base_task_ref"),
            ("head_sha", "head_task_ref"),
        ):
            actual = git(
                self.root,
                "--git-dir=" + str(self.remote),
                "rev-parse",
                "refs/heads/" + self.task[ref],
            )
            self.assertEqual(actual, self.task[key])
        retry = {**self.task, "captured_at": "2099-01-01T00:00:00Z"}
        g.enqueue(retry, self.gh, control, self.source)
        self.assertEqual(
            control.get("tasks/" + self.task["task_id"] + ".json"), self.task
        )
        minimum = g.trusted_minimum(self.task, control)
        self.assertEqual(minimum["version"], "impact/v5")
        self.assertEqual(minimum["required_checks"], ["control_plane"])
        self.assertEqual(minimum["impact"]["level"], "non_executable")

    def test_lifecycle_cancellation_reaches_gitee(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        for update in (
            {"draft": True},
            {"state": "closed"},
            {"body": body("Different summary")},
        ):
            self.gh.pull.update(update)
            self.assertFalse(g.is_current(self.gh, self.task))
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 1)
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 0)
        self.assertEqual(
            control.get("cancel/" + self.task["task_id"] + ".json")["task_id"],
            self.task["task_id"],
        )

    def test_status_comment_dashboard_order_has_no_return_channel(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        name = (
            "runs/v4/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json"
        )
        results.put({name: result})
        control_revision = git(control.root, "rev-parse", "HEAD")
        result_revision = git(results.root, "rev-parse", "HEAD")
        self.gh.writes.clear()
        write_bytes = Path.write_bytes

        def record_dashboard(path, data):
            if path.name == "v4-tasks.json":
                self.gh.writes.append("dashboard")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", record_dashboard):
            published = g.collect_results(
                self.gh, control, results, self.root / "dashboard"
            )
        self.assertEqual(self.gh.writes, ["status", "comment", "dashboard"])
        self.assertEqual(
            published[0]["result_digest"],
            hashlib.sha256((results.root / name).read_bytes()).hexdigest(),
        )
        self.assertEqual(self.gh.statuses[-1][1], "success")
        self.assertIn("Local CI", self.gh.comments[-1])
        before = (len(self.gh.statuses), len(self.gh.comments))
        again = g.collect_results(self.gh, control, results, self.root / "dashboard")
        self.assertEqual(again, [])
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))
        self.assertEqual(control_revision, git(control.root, "rev-parse", "HEAD"))
        self.assertEqual(result_revision, git(results.root, "rev-parse", "HEAD"))
        self.gh.pull["draft"] = True
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )

    def test_comment_failure_retries_same_uploaded_result(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {
                "runs/v4/"
                + self.task["task_id"]
                + "/"
                + result["run_id"]
                + "/result.json": result
            }
        )
        with patch.object(
            self.gh, "comment", side_effect=RuntimeError("comment unavailable")
        ):
            self.assertEqual(
                g.collect_results(self.gh, control, results, self.root / "dashboard"),
                [],
            )
        snapshot = json.loads((self.root / "dashboard/v4-tasks.json").read_text())
        self.assertEqual(snapshot["tasks"][0]["status"], "pass")
        self.assertEqual(snapshot["tasks"][0]["delivery_status"], "pending")
        self.assertEqual(self.gh.statuses[-1][1], "error")
        result_revision = git(results.root, "rev-parse", "HEAD")
        self.assertEqual(
            len(g.collect_results(self.gh, control, results, self.root / "dashboard")),
            1,
        )
        self.assertEqual(self.gh.statuses[-1][1], "success")
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(result_revision, git(results.root, "rev-parse", "HEAD"))

    def test_infrastructure_failure_reports_but_cancelled_results_do_not(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        result.update(
            status="infra_error",
            checks=[],
            required_checks=[],
            reviews={},
            unfinished=["environment failed"],
        )
        results.put(
            {
                "runs/v4/"
                + self.task["task_id"]
                + "/"
                + result["run_id"]
                + "/result.json": result
            }
        )
        with patch.object(
            g,
            "trusted_minimum",
            side_effect=AssertionError(
                "broken version need not be read for a failing result"
            ),
        ):
            published = g.collect_results(
                self.gh, control, results, self.root / "dashboard"
            )
        self.assertEqual(len(published), 1)
        self.assertEqual(self.gh.statuses[-1][1], "error")
        control.put(
            {
                "cancel/" + self.task["task_id"] + ".json": {
                    "task_id": self.task["task_id"],
                    "reason": "cancel after collection",
                }
            }
        )
        before = (len(self.gh.statuses), len(self.gh.comments))
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))

    def test_failed_dashboard_is_rebuilt_without_repeating_completed_writeback(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {f"runs/v4/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        dashboard = self.root / "dashboard"
        dashboard.write_text("not a directory")
        with self.assertRaises(OSError):
            g.collect_results(self.gh, control, results, dashboard)
        before = (len(self.gh.statuses), len(self.gh.comments))
        self.assertEqual(self.gh.statuses[-1][1], "success")
        dashboard.unlink()
        self.assertEqual(g.collect_results(self.gh, control, results, dashboard), [])
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))
        self.assertTrue((dashboard / "v4-tasks.json").is_file())

    def test_status_match_does_not_skip_missing_comment_repair(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        path = f"runs/v4/{self.task['task_id']}/{result['run_id']}/result.json"
        results.put({path: result})
        raw_digest = hashlib.sha256((results.root / path).read_bytes()).hexdigest()
        self.gh.status(
            self.task, "success", g.publication_description("pass", raw_digest)
        )
        status_count = len(self.gh.statuses)
        self.assertEqual(
            len(g.collect_results(self.gh, control, results, self.root / "dashboard")),
            1,
        )
        self.assertEqual(len(self.gh.statuses), status_count)
        self.assertEqual(len(self.gh.comments), 1)

    def test_head_change_during_status_write_skips_old_comment(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {f"runs/v4/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        status = self.gh.status

        def update_head(*args, **kwargs):
            status(*args, **kwargs)
            self.gh.pull["head"]["sha"] = "e" * 40

        with patch.object(self.gh, "status", side_effect=update_head):
            self.assertEqual(
                g.collect_results(self.gh, control, results, self.root / "dashboard"),
                [],
            )
        self.assertFalse(self.gh.comments)
        self.assertEqual(g.cancel_obsolete(self.gh, control), 1)
        self.assertEqual(self.gh.statuses[-1][1], "error")

    def test_invalid_result_and_expired_latest_run_never_reuse_old_pass(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        name = f"runs/v4/{self.task['task_id']}/{result['run_id']}/result.json"
        result["required_checks"] = []
        results.put({name: result})
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        self.assertEqual(self.gh.statuses[-1][1], "error")
        results.put({name: self.result()})
        newer = "20260908T120000Z-2"
        marker = {
            "task_id": self.task["task_id"],
            "run_id": newer,
            "result_digest": "a" * 64,
            "uploaded_at": "2026-09-08T12:00:00Z",
            "expired_at": "2026-10-08T12:00:00Z",
            "reason": "retention_expired",
        }
        results.put({f"retention/v4/{self.task['task_id']}/{newer}.json": marker})
        before = (len(self.gh.statuses), len(self.gh.comments))
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        snapshot = json.loads((self.root / "dashboard/v4-tasks.json").read_text())
        self.assertEqual(snapshot["tasks"][0]["status"], "expired")
        self.assertIsNone(snapshot["tasks"][0]["result"])
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))

    def test_invalid_results_cannot_claim_success(self):
        for mutate in (
            lambda r: r.update(required_checks=[]),
            lambda r: r["checks"][0].update(
                status="not_applicable", reason="AI chose to skip"
            ),
            lambda r: r["reviews"]["architecture"].update(evidence=[]),
            lambda r: r.update(blockers=["deterministic regression"]),
            lambda r: r.update(run_id="../../escape"),
            lambda r: r["task"].update(worker_revision_sha="e" * 40),
        ):
            result = copy.deepcopy(self.result())
            mutate(result)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                g.validate_result(result, self.task)

    def test_receiver_requires_observed_scope_instead_of_requested_parameters(self):
        result = self.result()
        result["required_checks"] = ["flaggems"]
        result["checks"][0].update(tool_id="flaggems", parameters={"mode": "full"})
        summary = self.execution_summary()
        summary["executions"][0].update(tool_id="flaggems", scope={"mode": "impact"})
        directory = self.root / result["run_id"]
        directory.mkdir()
        path = directory / "result.json"
        minimum = {
            "required_checks": ["flaggems"],
            "required_parameters": {"flaggems": {"mode": "full"}},
        }

        def save():
            raw = g.canonical(summary) + b"\n"
            (directory / "execution-summary.json").write_bytes(raw)
            result["execution_summary_sha256"] = hashlib.sha256(raw).hexdigest()
            path.write_bytes(g.canonical(result))

        save()
        with patch.object(g, "trusted_minimum", return_value=minimum):
            with self.assertRaisesRegex(ValueError, "required coverage"):
                g.read_result(path, self.task, None)
            summary["executions"][0]["scope"] = {"mode": "full"}
            save()
            self.assertEqual(g.read_result(path, self.task, None)[0]["status"], "pass")

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
        document = {
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "rules": [
                                {"id": "r", "properties": {"security-severity": "7.5"}}
                            ]
                        }
                    },
                    "results": [{"ruleId": "r", "level": "warning"}],
                }
            ]
        }
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
        calls, comments, statuses = [], [], {}

        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def transport(request, timeout):
            self.assertEqual(timeout, 30)
            method = request.get_method()
            path = request.full_url.split(f"/repos/{g.REPOSITORY}/", 1)[1]
            if path == "forbidden":
                raise g.HTTPError(request.full_url, 403, "permission denied", {}, None)
            if method == "GET":
                if path.startswith("commits/"):
                    sha = path.split("commits/", 1)[1].split("/", 1)[0]
                    return Response(statuses.get(sha, []))
                return Response(comments)
            data = json.loads(request.data)
            calls.append((method, path, data))
            if method == "POST" and path.endswith("/comments"):
                comments.append({"id": 11, "user": {"type": "Bot"}, **data})
            elif method == "POST" and path.startswith("statuses/"):
                statuses.setdefault(path.rsplit("/", 1)[1], []).insert(0, data)
            elif method == "PATCH":
                comments[0].update(data)
            return Response({})

        with patch.object(g, "urlopen", side_effect=transport):
            client = g.GitHub(
                g.REPOSITORY, "http://127.0.0.1", token="fake-local-token"
            )
            client.status(self.task, "pending", "Queued")
            client.comment(self.task, "first report")
            client.comment(self.task, "first report")
            client.comment(self.task, "updated report")
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[-1][0], "PATCH")
            self.assertTrue(comments[0]["body"].startswith(g.MARKER))
            self.assertTrue(
                client.status_matches(self.task, "pending", "Queued"), statuses
            )
            self.assertFalse(client.status_matches(self.task, "success", "Queued"))
            self.assertFalse(
                client.status_matches(self.task, "pending", "Different result")
            )
            self.assertEqual(calls[1][2]["context"], "local-ci/summary")
            statuses[self.head].insert(
                0,
                {
                    "context": "local-ci/summary",
                    "state": "error",
                    "description": "Newer failure",
                },
            )
            self.assertFalse(client.status_matches(self.task, "pending", "Queued"))
            with self.assertRaisesRegex(
                g.GitHubAPIError, "HTTP 403: permission denied"
            ) as error:
                client.request("forbidden")
            self.assertNotIn("token", str(error.exception))


class WorkflowStructureTests(unittest.TestCase):
    def test_workflow_gates_and_receiver_schedule(self):
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
            workflow = yaml.load(
                (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
            )
            self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(jobs["review-card"]["permissions"]["pull-requests"], "write")
        self.assertEqual(jobs["review-card"]["permissions"]["statuses"], "write")
        self.assertNotIn("schedule", data["on"])
        self.assertIn("LOCAL_CI_CONTROL_REF", worker_text)
        self.assertIn("LOCAL_CI_CONTROL_SHA", worker_text)
        receiver = yaml.load(
            (ROOT / ".github/workflows/ci-receiver.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(set(receiver["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(receiver["jobs"]["receive"]["timeout-minutes"], "10")
        self.assertEqual(
            set(g.REQUIRED_CONTEXTS),
            {"local-ci/basic", "local-ci/api", "local-ci/security", "local-ci/summary"},
        )


if __name__ == "__main__":
    unittest.main()
