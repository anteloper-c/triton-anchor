"""Recovery scenarios with actual files/Git; no simulated successful containers."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.common import digest, git, read_json, write_json  # noqa: E402
from runtime.poller import Poller  # noqa: E402


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "relay.git"
        self.seed = self.root / "seed"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.seed)], check=True, capture_output=True)
        git(self.seed, "config", "user.name", "CI recovery fixture")
        git(self.seed, "config", "user.email", "fixture@invalid")
        (self.seed / "README.md").write_text("Recovery protocol source fixture\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "source fixture")
        sha = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        self.task = {"schema": "triton-anchor-local-ci-task-metadata", "repository": "anteloper-c/triton-anchor",
                     "task_id": "recovery-task", "task_ref": "ci/push/main", "event_kind": "push", "pr_number": 0,
                     "target_branch": "main", "target_sha": sha, "tested_sha": sha, "head_sha": sha,
                     "base_sha": sha, "worker_revision_sha": sha, "captured_at": "2026-09-08T10:00:00Z"}
        git(self.seed, "push", "origin", f"{sha}:refs/heads/{self.task['task_ref']}")
        write_json(self.seed / "task-metadata.json", self.task)
        git(self.seed, "add", "task-metadata.json")
        git(self.seed, "commit", "-m", "task metadata")
        git(self.seed, "push", "origin", "HEAD:refs/heads/ci/meta/push/main")
        self.profile = {"id": "cpu", "triton_version": "3.2", "llvm_revision": "f" * 40,
                        "container": {"name": "ci-test-persistent", "image": "test-only", "healthcheck": ["true"]}}
        self.config = {"repository": self.task["repository"], "state_dir": str(self.root / "state"),
                       "workspace_host": str(self.root / "workspace"), "relay": {"url": str(self.remote)},
                       "docker": str(self.root / "deliberately-absent-docker"),
                       "profiles": [self.profile], "branch_profiles": {"main": "cpu"}, "codex": {}}
        self.poller = Poller(self.config)

    def pending(self, task_id=None, run_id="run-preserved"):
        task_id = task_id or self.task["task_id"]
        output = Path(self.config["state_dir"]) / "runs" / task_id / run_id
        output.mkdir(parents=True)
        result = {"schema": "triton-anchor-local-ci-result", "task_id": task_id, "run_id": run_id,
                  "tested_sha": self.task["tested_sha"], "conclusion": "success", "evidence": []}
        write_json(output / "result.json", result)
        (output / "report.md").write_text("Preserved, already-completed test result\n")
        write_json(output / "execution.json", {"phase": "publish_pending", "run_id": run_id, "profile_id": "cpu"})
        return output, result

    def test_restart_retries_saved_publication_without_rebuilding(self):
        seed, result = self.pending("seed-task", "seed-run")
        self.poller.relay.publish(seed, result)
        write_json(seed / "execution.json", {"phase": "published"})
        output, result = self.pending()
        before = digest(output / "result.json")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'publication unavailable' >&2\nexit 1\n", newline="\n")
        hook.chmod(0o755)
        with mock.patch.object(self.poller.engine, "run", side_effect=AssertionError("must not rebuild pending task")):
            self.poller.once()
        execution = read_json(output / "execution.json")
        self.assertEqual(execution["phase"], "publish_pending")
        self.assertGreater(execution["publish_attempts"], 0)
        self.assertEqual(digest(output / "result.json"), before)
        self.assertFalse((Path(self.config["state_dir"]) / "completed" / f"{self.task['task_id']}.json").exists())
        hook.unlink()
        restarted = Poller(self.config)
        with mock.patch.object(restarted.engine, "run", side_effect=AssertionError("restart must only publish saved results")):
            restarted.once()
        self.assertEqual(read_json(output / "execution.json")["phase"], "published")
        self.assertEqual(digest(output / "result.json"), before)
        self.assertTrue((Path(self.config["state_dir"]) / "completed" / f"{self.task['task_id']}.json").is_file())
        published = json.loads(git(self.remote, "show", f"local-ci-results:runs/{result['task_id']}/{result['run_id']}/result.json"))
        self.assertEqual(published, result)

    def test_corrupt_run_record_does_not_prevent_other_results_publication(self):
        bad = Path(self.config["state_dir"]) / "runs" / "a-corrupt-task" / "old-run" / "execution.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{truncated-json")
        output, _ = self.pending("z-valid-task")
        self.poller.retry_publications()
        self.assertEqual(read_json(output / "execution.json")["phase"], "published")

    def test_completed_publication_clears_pending_task_health(self):
        self.pending()
        self.poller.engine.heartbeat("publish_pending", self.task["task_id"], "prior transport failure")
        self.poller.retry_publications()
        task_health = read_json(Path(self.config["state_dir"]) / "health" / "task.json")
        self.assertEqual(task_health["state"], "completed")
        self.assertIsNone(task_health.get("error"))

    def test_actual_unavailable_docker_produces_publishable_error_without_lease(self):
        # This is a real executable-not-found error. No fake container lifecycle
        # is used or presented as successful deployment validation.
        output, result = self.poller.engine.run(self.task, self.profile)
        self.assertEqual(result["conclusion"], "error")
        self.assertTrue(result["blocking_reasons"])
        self.assertEqual(read_json(output / "execution.json")["phase"], "publish_pending")
        worker = Path(self.config["state_dir"]) / "workers" / "cpu.json"
        if worker.exists():
            self.assertIsNone(read_json(worker).get("lease"))


class AdmissionRejectionTests(unittest.TestCase):
    setUp = PublicationRecoveryTests.setUp

    def test_terminal_result_preserves_actual_validation_scope(self):
        for configured, expected in ((True, 'local_acceptance'), (False, 'production')):
            with self.subTest(local_acceptance=configured):
                self.poller.config['local_acceptance'] = configured
                result = self.poller.reject_task(self.task, 'scope validation fixture')
                self.assertEqual(result['validation_scope'], expected)
                self.assertFalse(result['control_identity']['verified'])
                self.assertEqual(result['conclusion'], 'error')

    def test_unknown_branch_rejection_is_published_once_and_survives_restart(self):
        self.config['branch_profiles'] = {}
        with mock.patch.object(self.poller.engine, 'run', side_effect=AssertionError('no environment for unknown branch')):
            outcomes = self.poller.once()
        self.assertEqual([r['conclusion'] for r in outcomes], ['error'])
        result = outcomes[0]
        self.assertEqual(result['task_id'], self.task['task_id'])
        self.assertEqual(result['tested_sha'], self.task['tested_sha'])
        self.assertIn('No trusted environment profile', result['blocking_reasons'][0])
        self.assertFalse(result['evidence'])
        self.assertFalse(any(c['status'] == 'passed' for c in result['checks']))
        rejected = Path(self.config['state_dir']) / 'rejected' / (self.task['task_id'] + '.json')
        before = rejected.read_bytes()
        published = json.loads(git(self.remote, 'show', f"local-ci-results:runs/{self.task['task_id']}/admission/result.json"))
        self.assertEqual(published, result)
        from scripts.dashboard.sync_agent_results import normalize_result
        dashboard = normalize_result(published, f"runs/{self.task['task_id']}/admission/result.json", '',
                                     'local-ci-results', {self.task['task_id']: 'admission'})
        self.assertEqual(dashboard['blocking_reasons'], result['blocking_reasons'])
        restarted = Poller(self.config)
        with mock.patch.object(restarted.engine, 'run', side_effect=AssertionError('terminal task must not rerun')):
            self.assertEqual(restarted.once(), [])
        self.assertEqual(rejected.read_bytes(), before)

    def test_stale_task_is_recorded_before_profile_lookup(self):
        class UnreachableProfiles(dict):
            def get(self, *args):
                raise AssertionError('stale task must not select an environment')
        self.config['branch_profiles'] = UnreachableProfiles()
        with mock.patch.object(self.poller.relay, 'current', return_value=False), \
             mock.patch.object(self.poller.engine, 'run', side_effect=AssertionError('stale task cannot run')):
            outcomes = self.poller.once()
        self.assertEqual(outcomes[0]['conclusion'], 'cancelled')
        record = read_json(Path(self.config['state_dir']) / 'rejected' / (self.task['task_id'] + '.json'))
        self.assertEqual(record['head_sha'], self.task['head_sha'])
        self.assertEqual(record['conclusion'], 'cancelled')

    def test_rejection_publish_failure_retries_same_bytes_without_reexecution(self):
        self.config['branch_profiles'] = {}
        with mock.patch.object(self.poller.relay, 'publish', side_effect=RuntimeError('temporary publication outage')), \
             mock.patch.object(self.poller.engine, 'run', side_effect=AssertionError('no task execution')):
            self.poller.once()
        output = Path(self.config['state_dir']) / 'runs' / self.task['task_id'] / 'admission'
        before = (output / 'result.json').read_bytes()
        self.assertEqual(read_json(output / 'execution.json')['phase'], 'publish_pending')
        restarted = Poller(self.config)
        with mock.patch.object(restarted.engine, 'run', side_effect=AssertionError('only retry publication')):
            restarted.once()
        self.assertEqual((output / 'result.json').read_bytes(), before)
        self.assertEqual(read_json(output / 'execution.json')['phase'], 'published')


if __name__ == "__main__":
    unittest.main()
