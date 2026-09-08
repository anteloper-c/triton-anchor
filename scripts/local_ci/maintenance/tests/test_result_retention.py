from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL))
from agent_ci.protocol import RESULT_SCHEMA, ContractError, canonical
from agent_ci.relay import GitRelay
from maintenance.retain_results import retain_results


class ResultRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "relay.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.remote)], check=True)
        self.relay = GitRelay(str(self.remote), self.root / "client", allow_local=True)
        self.task = "a" * 64
        self.old = f"runs/v4/{self.task}/old-run"
        self.recent = f"runs/v4/{self.task}/new-run"
        self.now = 1788825600  # 2026-09-08 UTC

    def publish(self, prefix, run_id, date):
        self.relay.env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.relay.write("local-ci-results", {
            prefix + "/result.json": canonical({"schema": RESULT_SCHEMA, "task": {"task_id": self.task}, "run_id": run_id}),
            prefix + "/evidence/log.txt": b"real local Git fixture, no model/backend execution",
        }, immutable=True)

    def test_fixed_retention_dry_run_then_delete_without_receipt(self):
        self.publish(self.old, "old-run", "2026-07-01T00:00:00Z")
        self.publish(self.recent, "new-run", "2026-09-07T00:00:00Z")
        self.relay.write("local-ci-results", {"legacy/results.json": b"historical"})
        self.relay.write("local-ci-control", {"tasks/preserve.json": b"{}"})
        self.relay.refresh()
        head = self.relay.ref_sha("local-ci-results")
        control = self.relay.ref_sha("local-ci-control")
        report = retain_results(self.relay, now=self.now)
        self.assertFalse(report["applied"])
        self.assertEqual(["old-run"], [x["run_id"] for x in report["expired"]])
        self.relay.refresh()
        self.assertEqual(head, self.relay.ref_sha("local-ci-results"))
        report = retain_results(self.relay, now=self.now, apply=True)
        self.relay.refresh()
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/result.json"))
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/evidence/log.txt"))
        self.assertIsNotNone(self.relay.read("local-ci-results", self.recent + "/result.json"))
        self.assertEqual(b"historical", self.relay.read("local-ci-results", "legacy/results.json"))
        marker = self.relay.read_json("local-ci-results", f"retention/v4/{self.task}/old-run.json")
        self.assertEqual(report["expired"][0], marker)
        self.assertEqual(control, self.relay.ref_sha("local-ci-control"))
        self.assertEqual([], retain_results(self.relay, now=self.now, apply=True)["expired"])

    def test_invalid_identity_is_preserved_and_reported(self):
        self.relay.env.update(GIT_AUTHOR_DATE="2026-07-01T00:00:00Z", GIT_COMMITTER_DATE="2026-07-01T00:00:00Z")
        self.relay.write("local-ci-results", {self.old + "/result.json": json.dumps({"run_id": "different"}).encode()})
        report = retain_results(self.relay, now=self.now, apply=True)
        self.assertFalse(report["expired"])
        self.assertEqual("invalid_result_identity", report["skipped"][0]["reason"])
        self.relay.refresh()
        self.assertIsNotNone(self.relay.read("local-ci-results", self.old + "/result.json"))

    def test_invalid_policy_or_other_branch_cannot_delete(self):
        for days in (0, -1, True, "30"):
            with self.assertRaises(ValueError):
                retain_results(self.relay, days, apply=True)
        self.relay.results_branch = "main"
        with self.assertRaises(ContractError):
            retain_results(self.relay, apply=True)

    def saved_run(self):
        directory = self.root / "saved-old-run"
        directory.mkdir()
        (directory / "evidence").mkdir()
        raw = canonical({"schema": RESULT_SCHEMA, "task": {"task_id": self.task}, "run_id": "old-run"})
        (directory / "result.json").write_bytes(raw)
        (directory / "evidence/log.txt").write_bytes(b"real local Git fixture, no model/backend execution")
        return directory, hashlib.sha256(raw).hexdigest()

    def test_expired_upload_replay_succeeds_without_recreating_or_rewriting(self):
        self.publish(self.old, "old-run", "2026-07-01T00:00:00Z")
        retain_results(self.relay, now=self.now, apply=True)
        self.relay.refresh()
        head = self.relay.ref_sha("local-ci-results")
        marker_path = f"retention/v4/{self.task}/old-run.json"
        marker = self.relay.read("local-ci-results", marker_path)
        directory, digest = self.saved_run()
        returned = self.relay.publish_result({"task_id": self.task}, "old-run", directory)
        self.assertEqual(digest, returned)
        self.relay.refresh()
        self.assertEqual(head, self.relay.ref_sha("local-ci-results"))
        self.assertEqual(marker, self.relay.read("local-ci-results", marker_path))
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/result.json"))
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/evidence/log.txt"))
        self.assertEqual([], retain_results(self.relay, now=self.now + 31 * 86400, apply=True)["expired"])

    def test_expired_replay_rejects_changed_result_and_invalid_marker(self):
        self.publish(self.old, "old-run", "2026-07-01T00:00:00Z")
        retain_results(self.relay, now=self.now, apply=True)
        directory, _ = self.saved_run()
        original = (directory / "result.json").read_bytes()
        (directory / "result.json").write_bytes(original + b"\n")
        with self.assertRaisesRegex(ContractError, "does not match"):
            self.relay.publish_result({"task_id": self.task}, "old-run", directory)
        (directory / "result.json").write_bytes(original)
        self.relay.refresh()
        marker_path = f"retention/v4/{self.task}/old-run.json"
        original_marker = self.relay.read_json("local-ci-results", marker_path)
        for key, value in (("task_id", "b" * 64), ("run_id", "other"), ("reason", "other"),
                           ("result_digest", "0" * 64), ("schema", "other")):
            with self.subTest(field=key):
                self.relay.write("local-ci-results", {marker_path: canonical({**original_marker, key: value})})
                self.relay.refresh()
                head = self.relay.ref_sha("local-ci-results")
                with self.assertRaisesRegex(ContractError, "does not match"):
                    self.relay.publish_result({"task_id": self.task}, "old-run", directory)
                self.relay.refresh()
                self.assertEqual(head, self.relay.ref_sha("local-ci-results"))
                self.assertIsNone(self.relay.read("local-ci-results", self.old + "/result.json"))

    def test_concurrent_new_upload_is_preserved_after_retention_push_retry(self):
        self.publish(self.old, "old-run", "2026-07-01T00:00:00Z")
        publisher = GitRelay(str(self.remote), self.root / "publisher", allow_local=True)
        publisher.env.update(GIT_AUTHOR_DATE="2026-09-08T00:00:00Z", GIT_COMMITTER_DATE="2026-09-08T00:00:00Z")
        original_git, pushes = self.relay.git, []

        def racing_git(args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    publisher.write("local-ci-results", {self.recent + "/result.json": canonical(
                        {"schema": RESULT_SCHEMA, "task": {"task_id": self.task}, "run_id": "new-run"})}, immutable=True)
            return original_git(args, **kwargs)

        self.relay.git = racing_git
        report = retain_results(self.relay, now=self.now, apply=True)
        self.assertEqual(2, len(pushes))
        self.assertEqual(["old-run"], [entry["run_id"] for entry in report["expired"]])
        self.relay.refresh()
        self.assertIsNotNone(self.relay.read("local-ci-results", self.recent + "/result.json"))
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/result.json"))

    def test_upload_retry_rechecks_expiry_after_concurrent_retention_push(self):
        self.publish(self.old, "old-run", "2026-07-01T00:00:00Z")
        cleaner = GitRelay(str(self.remote), self.root / "cleaner", allow_local=True)
        original_git, pushes = self.relay.git, []

        def racing_git(args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    retain_results(cleaner, now=self.now, apply=True)
            return original_git(args, **kwargs)

        self.relay.git = racing_git
        # Batch an unchanged old run with a newly produced run. The first push
        # loses to cleanup, so the retry must omit all of the expired run files.
        self.relay.write("local-ci-results", {
            self.old + "/result.json": canonical({"schema": RESULT_SCHEMA, "task": {"task_id": self.task}, "run_id": "old-run"}),
            self.old + "/evidence/log.txt": b"real local Git fixture, no model/backend execution",
            self.recent + "/result.json": canonical({"schema": RESULT_SCHEMA, "task": {"task_id": self.task}, "run_id": "new-run"}),
        }, immutable=True)
        self.assertEqual(2, len(pushes))
        self.relay.refresh()
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/result.json"))
        self.assertIsNone(self.relay.read("local-ci-results", self.old + "/evidence/log.txt"))
        self.assertIsNotNone(self.relay.read("local-ci-results", self.recent + "/result.json"))
        self.assertIsNotNone(self.relay.read("local-ci-results", f"retention/v4/{self.task}/old-run.json"))


if __name__ == "__main__":
    unittest.main()
