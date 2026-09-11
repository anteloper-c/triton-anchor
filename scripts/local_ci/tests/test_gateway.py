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
    "gateway", ROOT / "scripts/ci/gateway.py"
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
        return result

    def result(self):
        return {
            "schema": g.RESULT_SCHEMA,
            "task": self.task,
            "run_id": "20260907T120000Z-1",
            "status": "pass",
            "summary": "Documentation and architecture review completed.",
            "checks": [{
                "tool_id": "control_plane", "status": "pass",
                "summary": "Validated", "evidence": [],
            }],
            "reviews": [
                {"kind": "pr_info", "status": "pass", "summary": "Clear", "evidence": []},
                {
                "kind": "architecture", "status": "pass",
                "summary": "Compatible", "evidence": ["README.md"],
            },
            ],
            "findings": [],
            "blocking_reasons": [],
            "environment": {"profile": "local"},
            "policy": {"required_checks": ["control_plane"]},
            "artifacts": [],
            "completed_at": "2026-09-07T12:00:00Z",
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
        for branch in ("main", "CI_dev_forPR", "release/next"):
            with self.subTest(branch=branch):
                self.gh.pull["base"]["ref"] = branch
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(task["target_branch"], branch)
        self.gh.pull["base"]["ref"] = "main"
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

    def test_preinstalled_flaggems_and_other_submodule_mirrors(self):
        links = [{"path": "FlagGems", "sha": "c" * 40}]
        with patch.object(self.gh, "gitlinks", return_value=links):
            with patch.dict(g.os.environ, {"GITEE_SUBMODULE_MIRRORS": "{}"}):
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(task["submodules"], [])
            for path in ("OtherDependency", "vendor/FlagGems"):
                with self.subTest(path=path):
                    links[:] = [
                        {"path": "FlagGems", "sha": "c" * 40},
                        {"path": path, "sha": "d" * 40},
                    ]
                    with patch.dict(
                        g.os.environ, {"GITEE_SUBMODULE_MIRRORS": "{}"}
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "Gitee submodule mirror"
                        ):
                            g.prepare_task(self.gh, self.base, 7)
                    with patch.dict(
                        g.os.environ,
                        {
                            "GITEE_SUBMODULE_MIRRORS": json.dumps(
                                {path: "https://gitee.com/test/dependency.git"}
                            )
                        },
                    ):
                        task = g.prepare_task(self.gh, self.base, 7)
                    self.assertEqual(
                        {row["path"] for row in task["submodules"]}, {path}
                    )
                    self.assertEqual(
                        {row["variant"] for row in task["submodules"]},
                        {"candidate", "base"},
                    )
                    self.assertTrue(
                        all(
                            task["task_id"] in row["task_ref"]
                            for row in task["submodules"]
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

    def test_legacy_current_does_not_block_new_dispatch_or_collection(self):
        control = self.store(g.CONTROL_BRANCH)
        legacy = {
            **self.task,
            "schema": g.TASK_SCHEMA + "/v4",
            "pr_number": 8,
            "worker_revision_sha": "f" * 40,
            "task_ref": "ci/pr-8/docs-topic",
            "base_task_ref": "ci/base/pr-8/docs-topic",
            "head_task_ref": "ci/head/pr-8/docs-topic",
        }
        legacy["task_id"] = g.digest({key: legacy[key] for key in g.IDENTITY_FIELDS})
        control.put(
            {
                f"tasks/{legacy['task_id']}.json": legacy,
                f"current/{g.current_key(legacy)}.json": {
                    "task_id": legacy["task_id"]
                },
            }
        )
        results = self.store(g.RESULTS_BRANCH)
        before = git(control.root, "rev-parse", "HEAD")
        self.assertEqual(g.cancel_obsolete(self.gh, control, 8), 0)
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        self.assertEqual(git(control.root, "rev-parse", "HEAD"), before)
        self.assertEqual(self.gh.writes, [])

        g.enqueue(self.task, self.gh, control, self.source)
        result = self.result()
        results.put(
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        self.assertEqual(g.cancel_obsolete(self.gh, control), 0)
        published = g.collect_results(
            self.gh, control, results, self.root / "dashboard"
        )
        self.assertEqual([row["task_id"] for row in published], [self.task["task_id"]])
        self.assertEqual(self.gh.statuses[-1][1], "success")
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text())
        self.assertEqual(
            [row["task"]["task_id"] for row in snapshot["tasks"]],
            [self.task["task_id"]],
        )
        self.assertEqual(control.get(f"tasks/{legacy['task_id']}.json"), legacy)
        self.assertIsNone(control.get(f"cancel/{legacy['task_id']}.json"))

    def test_damaged_current_is_not_ignored_as_legacy(self):
        control = self.store(g.CONTROL_BRANCH)
        results = self.store(g.RESULTS_BRANCH)
        for mutation in (
            {"task_ref": self.task["task_ref"] + "-damaged"},
            {"task_ref": "ci/pr-7/docs-topic"},
            {"title": "Metadata changed without updating its identity"},
        ):
            with self.subTest(mutation=mutation):
                damaged = {**self.task, **mutation}
                control.put(
                    {
                        f"tasks/{self.task['task_id']}.json": damaged,
                        f"current/{g.current_key(self.task)}.json": {
                            "task_id": self.task["task_id"]
                        },
                    }
                )
                with self.assertRaises(ValueError):
                    g.cancel_obsolete(self.gh, control, 7)
                with self.assertRaises(ValueError):
                    g.collect_results(
                        self.gh, control, results, self.root / "dashboard"
                    )
        self.assertEqual(self.gh.writes, [])

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
            "runs/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json"
        )
        results.put({name: result})
        control_revision = git(control.root, "rev-parse", "HEAD")
        result_revision = git(results.root, "rev-parse", "HEAD")
        self.gh.writes.clear()
        write_bytes = Path.write_bytes

        def record_dashboard(path, data):
            if path.name == "tasks.json":
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
                "runs/"
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
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text())
        self.assertEqual(snapshot["tasks"][0]["status"], "infra_error")
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
            reviews=[],
            blocking_reasons=["environment failed"],
        )
        results.put(
            {
                "runs/"
                + self.task["task_id"]
                + "/"
                + result["run_id"]
                + "/result.json": result
            }
        )
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
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
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
        self.assertTrue((dashboard / "tasks.json").is_file())

    def test_status_match_does_not_skip_missing_comment_repair(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        path = f"runs/{self.task['task_id']}/{result['run_id']}/result.json"
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
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
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

    def test_latest_invalid_result_does_not_reuse_an_old_pass(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        prefix = f"runs/{self.task['task_id']}"
        results.put({f"{prefix}/{result['run_id']}/result.json": result})
        newer = {**result, "run_id": "20260908T120000Z-2", "blocking_reasons": ["Unresolved"]}
        results.put({f"{prefix}/{newer['run_id']}/result.json": newer})
        self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])
        self.assertEqual(self.gh.statuses[-1][1], "error")
        self.assertFalse(self.gh.comments)

    def test_invalid_results_cannot_claim_success(self):
        for mutate in (
            lambda result: result.update(blocking_reasons=["deterministic regression"]),
            lambda result: result.update(run_id="../../escape"),
            lambda result: result["task"].update(worker_revision_sha="e" * 40),
        ):
            result = copy.deepcopy(self.result())
            mutate(result)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                g.validate_result(result, self.task)

    def test_receiver_reads_selected_files_from_same_git_result(self):
        result = self.result()
        result["artifacts"] = [{"path": "report.txt", "size": 2}]
        directory = self.root / result["run_id"]
        directory.mkdir()
        path = directory / "result.json"
        path.write_bytes(g.canonical(result))
        with self.assertRaises(ValueError):
            g.read_result(path, self.task, None)
        (directory / "artifacts").mkdir()
        (directory / "artifacts/report.txt").write_text("ok")
        self.assertEqual(g.read_result(path, self.task, None)[0], result)

    def test_closed_pr_finishes_pending_checks_without_dispatched_task(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        writes = []
        pending = {"name": "local-ci/basic", "status": "queued", "id": 12,
                   "external_id": "triton-anchor-ci-v4:basic:old",
                   "app": {"slug": "github-actions"}}
        completed = {**pending, "id": 13, "status": "completed"}

        def request(path, method="GET", data=None):
            if method != "GET":
                writes.append((path, data))
                return {}
            if path == "pulls/7":
                return {**self.gh.pull, "state": "closed"}
            if "/statuses?" in path:
                return [{"context": "local-ci/summary", "state": "pending"}]
            if "/check-runs?" in path:
                return {"check_runs": [pending, completed]}
            raise AssertionError(path)

        with patch.object(gh, "request", side_effect=request):
            self.assertTrue(gh.finish_inactive_pr(7))
        self.assertEqual([path for path, _ in writes], [f"statuses/{self.head}", "check-runs/12"])
        self.assertEqual(writes[0][1]["state"], "error")
        self.assertEqual(writes[1][1]["conclusion"], "cancelled")

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
        self.assertIn("worker_revision_sha", data["on"]["workflow_dispatch"]["inputs"])
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
