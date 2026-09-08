"""Recovery scenarios with actual files/Git; no simulated successful containers."""
from __future__ import annotations

import json
import base64
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
from runtime.engine import Engine  # noqa: E402
from runtime.result_paths import run_relative  # noqa: E402


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
        result = {**{k: self.task[k] for k in ('event_kind', 'pr_number', 'target_branch', 'task_ref')},
                  "schema": "triton-anchor-local-ci-result", "task_id": task_id, "run_id": run_id,
                  "tested_sha": self.task["tested_sha"], "conclusion": "success", "evidence": []}
        output = Path(self.config["state_dir"]) / run_relative(result, run_id)
        output.mkdir(parents=True)
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
        published = json.loads(git(self.remote, "show", 'local-ci-results:' + run_relative(result, result['run_id']) + '/result.json'))
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

    def test_grouped_publications_keep_queue_fault_visible_until_all_are_published(self):
        from maintenance.health import collect
        from maintenance.watchdog import evaluate
        from maintenance.notify import public_faults

        with mock.patch.dict(self.task, event_kind='pull_request', pr_number=4,
                             task_ref='ci/pr-4/contributor/topic'):
            failed, failed_result = self.pending('pr-task')
        completed, completed_result = self.pending('push-task')
        original_bytes = (failed / 'result.json').read_bytes()
        publish = self.poller.relay.publish

        def transport(output, result):
            if result['task_id'] == 'pr-task':
                raise RuntimeError('PR publication channel temporarily unavailable')
            return publish(output, result)

        config = {**self.config, 'worker_id': 'fixture-host', 'health': {'min_free_gb': 0}}
        manager = mock.Mock()
        manager.inspect.return_value = {'running': True}
        with mock.patch.object(self.poller.relay, 'publish', side_effect=transport):
            self.assertTrue(self.poller.retry_publications())
        self.assertEqual(read_json(failed / 'execution.json')['phase'], 'publish_pending')
        self.assertEqual(read_json(completed / 'execution.json')['phase'], 'published')
        self.assertEqual(json.loads(git(self.remote, 'show', 'local-ci-results:' +
                         run_relative(completed_result, completed_result['run_id']) + '/result.json')),
                         completed_result)
        # Health and the independent watchdog use the queue heartbeat, without
        # depending on flat or grouped report directory depths.
        snapshot = collect(config, manager)
        faults = evaluate(snapshot, config['worker_id'])
        self.assertIn('poller_publish_pending', {row['code'] for row in faults})
        self.assertTrue(any('等待发布' in row for row in public_faults(faults)))

        self.assertFalse(self.poller.retry_publications())
        self.assertEqual((failed / 'result.json').read_bytes(), original_bytes)
        self.assertEqual(read_json(failed / 'execution.json')['phase'], 'published')
        self.assertEqual(evaluate(collect(config, manager), config['worker_id']), [])
        self.assertEqual(json.loads(git(self.remote, 'show', 'local-ci-results:' +
                         run_relative(failed_result, failed_result['run_id']) + '/result.json')),
                         failed_result)

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


class PreparationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = {'id': 'fixture', 'triton_version': '3.3', 'container': {'name': 'persistent-fixture'}}
        config = {'state_dir': str(self.root / 'state'), 'workspace_host': str(self.root / 'workspace'),
                  'local_acceptance': True, 'codex': {}}
        self.engine = Engine(config, mock.Mock(), mock.Mock())
        self.output = self.root / 'output'
        self.output.mkdir()
        self.engine.preparation_record = self.output / 'preparation.json'
        self.engine.preparation_task_id = 'prepare-task'

    def process(self, returncode=0):
        process = mock.Mock(returncode=returncode)
        process.communicate.return_value = ('prepared', 'fixture error')
        process.poll.return_value = returncode
        return process

    def task(self):
        return {'schema': 'triton-anchor-local-ci-task-metadata', 'repository': 'anteloper-c/triton-anchor',
                'task_id': 'prepare-task', 'task_ref': 'ci/push/main', 'event_kind': 'push', 'pr_number': 0,
                'target_branch': 'main', 'target_sha': 'a' * 40, 'tested_sha': 'a' * 40, 'head_sha': 'a' * 40,
                'base_sha': 'a' * 40, 'worker_revision_sha': 'b' * 40, 'captured_at': '2026-09-08T10:00:00Z'}

    def test_long_preparation_has_internal_deadline_and_fresh_heartbeat(self):
        process = self.process()
        process.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 20), ('prepared', '')]
        with mock.patch('runtime.engine.subprocess.Popen', return_value=process) as spawn, \
             mock.patch.object(self.engine, 'stop_preparation') as stop:
            self.assertEqual(self.engine.docker_run(self.profile, 'cp', '-a', '/opt/ci-venv', '/workspace/venv'), 'prepared')
        spec = json.loads(base64.urlsafe_b64decode(spawn.call_args.args[0][-1]))
        self.assertEqual(spec['timeout'], 300)
        self.assertNotIn('env', spec)
        self.assertNotIn('env', spawn.call_args.kwargs)
        self.assertEqual(read_json(self.engine.state / 'health/task.json')['phase'], 'preparing')
        self.assertEqual(process.communicate.call_count, 2)
        self.assertFalse(self.engine.preparation_record.exists())
        stop.assert_not_called()

    def test_host_deadline_stops_exact_spec_and_reaps_client(self):
        process = self.process()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired('fixture', 20), ('', '')]
        self.engine.config['preparation_timeout'] = 5
        with mock.patch('runtime.engine.subprocess.Popen', return_value=process), \
             mock.patch('runtime.engine.time.monotonic', side_effect=[0, 0, 35]), \
             mock.patch.object(self.engine, 'stop_preparation') as stop:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.engine.docker_run(self.profile, 'cp', '-a', '/seed', '/target')
        self.assertEqual(stop.call_args.args[1]['spec']['argv'], ['cp', '-a', '/seed', '/target'])
        self.assertEqual(stop.call_args.args[1]['spec']['timeout'], 5)
        process.kill.assert_called_once()
        self.assertFalse(self.engine.preparation_record.exists())

    def test_unconfirmed_stop_preserves_spec_for_recovery(self):
        process = self.process(returncode=1)
        with mock.patch('runtime.engine.subprocess.Popen', return_value=process), \
             mock.patch.object(self.engine, 'stop_preparation', side_effect=RuntimeError('unreachable')):
            with self.assertRaisesRegex(RuntimeError, 'lease retained'):
                self.engine.docker_run(self.profile, 'cp', '-a', '/seed', '/target')
        saved = read_json(self.engine.preparation_record)
        self.assertEqual(saved['spec']['argv'], ['cp', '-a', '/seed', '/target'])
        self.assertEqual(saved['user'], '0')
        self.assertTrue(self.engine.preparation_cleanup_failed)

    def test_interrupted_copy_without_ready_marker_is_never_reused(self):
        task = self.root / 'task'
        task.mkdir()
        def copy_then_fail(profile, *args):
            if args[0] == 'cp':
                (task / 'venv').mkdir()
            else:
                raise RuntimeError('permission step interrupted')
        with mock.patch.object(self.engine, 'docker_run', side_effect=copy_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'permission step interrupted'):
                self.engine.prepare_venv(self.profile, task, '/workspace/task', self.output)
        self.assertFalse((self.output / 'venv-ready.json').exists())
        with mock.patch.object(self.engine, 'docker_run') as execute:
            with self.assertRaisesRegex(RuntimeError, 'copy is incomplete'):
                self.engine.prepare_venv(self.profile, task, '/workspace/task', self.output)
        execute.assert_not_called()

    def test_ready_marker_is_written_after_copy_and_permissions_then_reused(self):
        task = self.root / 'task'
        task.mkdir()
        def prepare(profile, *args):
            self.assertFalse((self.output / 'venv-ready.json').exists())
            if args[0] == 'cp':
                (task / 'venv').mkdir()
        with mock.patch.object(self.engine, 'docker_run', side_effect=prepare) as execute:
            self.engine.prepare_venv(self.profile, task, '/workspace/task', self.output)
            self.assertEqual(execute.call_count, 2)
            self.engine.prepare_venv(self.profile, task, '/workspace/task', self.output)
            self.assertEqual(execute.call_count, 2)

    def test_resume_stops_saved_preparation_before_work_and_retains_failed_lease(self):
        task = self.task()
        record = self.engine.state / run_relative(task, 'preserved') / 'execution.json'
        write_json(record, {'run_id': 'preserved'})
        saved = {'spec': {'id': 'exact-old-invocation'}}
        write_json(record.with_name('preparation.json'), saved)
        with mock.patch.object(self.engine, 'stop_preparation', side_effect=RuntimeError('stop unavailable')) as stop:
            with self.assertRaisesRegex(RuntimeError, 'stop unavailable'):
                self.engine.run(task, self.profile, resume_record=record)
        self.engine.manager.acquire.assert_called_once_with(self.profile, task['task_id'])
        stop.assert_called_once_with(self.profile, saved)
        self.engine.manager.release.assert_not_called()
        self.engine.relay.checkout.assert_not_called()
        self.assertEqual(read_json(record.with_name('preparation.json')), saved)

    def test_failed_root_cleanup_prevents_finally_releasing_worker(self):
        relay = self.engine.relay
        relay.current.return_value, relay.changed_paths.return_value = True, ['README.md']
        def checkout(sha, path):
            path.mkdir()
            (path / 'README.md').write_text('frozen fixture')
            return {'README.md': digest(path / 'README.md')}
        relay.checkout.side_effect = checkout
        def failed_preparation(*args):
            self.engine.preparation_cleanup_failed = True
            raise RuntimeError('root process cleanup unconfirmed')
        with mock.patch.object(self.engine, 'docker_run', side_effect=failed_preparation) as execute, \
             mock.patch('runtime.control.verify_control', return_value={'tree_sha256': 'fixture', 'verified': False}), \
             mock.patch('runtime.control.verify_container_control', return_value={'verified': False}):
            output, result = self.engine.run(self.task(), self.profile)
        self.engine.manager.acquire.assert_called_once()
        self.engine.manager.release.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(result['conclusion'], 'error')
        self.assertTrue(any('lease retained' in reason for reason in result['blocking_reasons']))


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
        relative = run_relative(self.task, 'admission') + '/result.json'
        published = json.loads(git(self.remote, 'show', 'local-ci-results:' + relative))
        self.assertEqual(published, result)
        from scripts.dashboard.sync_agent_results import normalize_result
        dashboard = normalize_result(published, relative, '',
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
        output = Path(self.config['state_dir']) / run_relative(self.task, 'admission')
        before = (output / 'result.json').read_bytes()
        self.assertEqual(read_json(output / 'execution.json')['phase'], 'publish_pending')
        restarted = Poller(self.config)
        with mock.patch.object(restarted.engine, 'run', side_effect=AssertionError('only retry publication')):
            restarted.once()
        self.assertEqual((output / 'result.json').read_bytes(), before)
        self.assertEqual(read_json(output / 'execution.json')['phase'], 'published')


if __name__ == "__main__":
    unittest.main()
