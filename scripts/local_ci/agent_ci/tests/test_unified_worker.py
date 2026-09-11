"""Real file/Git/Worker integration; Docker and model calls are test boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LOCAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LOCAL_ROOT))
from agent_ci.protocol import (
    ContractError,
    TASK_SCHEMA,
    canonical,
    current_key,
    metadata_digest,
    task_id,
    validate_delivery,
    validate_execution_summary,
)
from agent_ci.relay import GitRelay
from agent_ci.worker import Worker
from agent_ci.tests.support import ReleaseBoundary, git


class ContainerBoundary:
    def __init__(self):
        self.acquired, self.destroyed, self.handles = [], [], {}
        self.cancel_event = None

    def acquire_task(self, task, run_id, *, rpc_directory):
        self.acquired.append(run_id)
        handle = {
            "task_id": task["task_id"],
            "run_id": run_id,
            "attempt_id": run_id,
            "generation": run_id,
            "environment_fingerprint": "e" * 64,
            "profile": "simulated",
            "backend_enabled": False,
            "image_id": "simulated-image",
            "state": "active",
        }
        self.handles[run_id] = handle
        return handle

    def generations(self):
        return self.handles

    def collect_retired(self):
        pass

    def export_execution(self, *args):
        return {"evidence_loss": False}

    def purge_credentials(self, handle):
        pass

    def stop_task(self, handle):
        return {"verified": True}

    def destroy_task(self, handle, keep_data=False):
        handle["state"] = "removed"
        self.destroyed.append(handle["run_id"])


class LocalExecutionBoundary:
    calls = []

    def __init__(self, config, state_dir, generation, task, relay, *, manager=None):
        self.config, self.state_dir, self.generation = (
            config,
            Path(state_dir),
            generation,
        )
        self.task, self.relay, self.manager = task, relay, manager
        self.run_dir = self.state_dir / "runs" / task["task_id"] / generation["run_id"]

    def prepare(self, variant="candidate"):
        destination = (
            self.state_dir
            / "work"
            / self.task["task_id"]
            / self.generation["run_id"]
            / variant
        )
        sha = self.task["tested_sha" if variant == "candidate" else "base_sha"]
        self.relay.checkout(sha, destination)
        self.relay.checkout_submodules(self.task, sha, destination)
        return destination

    def plan_context(self, execution_id, variant, parameters):
        return {
            "task_id": self.task["task_id"],
            "source_dir": str(self.prepare(variant)),
            "artifact_dir": str(self.run_dir / "artifacts" / execution_id),
            "target_sha": self.task["tested_sha"],
            "triton_version": "3.0.0",
            "profile": {"backend_enabled": False, "tools": {}},
        }

    def run(self, tool_id, execution_id, variant, parameters, cancelled, custom=None):
        self.calls.append((self.generation["run_id"], tool_id))
        output = self.run_dir / "artifacts" / execution_id / tool_id
        output.mkdir(parents=True)
        log = self.run_dir / "logs" / (execution_id + ".log")
        # A real local command produces persistent evidence. No compiler success
        # is claimed: this models the control-plane check at the Docker boundary.
        completed = subprocess.run(
            [sys.executable, "-c", "print('control fixture passed')"],
            capture_output=True,
        )
        log.write_bytes(completed.stdout)
        (output / "junit.xml").write_text(
            '<testsuite tests="1" failures="0"><testcase name="control"/></testsuite>'
        )
        if self.config.get("include_optional_evidence"):
            (output / "optional.whl").write_bytes(b"OPTIONAL-FIXTURE wheel boundary")
        return {
            "execution_id": execution_id,
            "status": "pass",
            "reason": "",
            "exit_code": completed.returncode,
            "original_subject": True,
            "environment_fingerprint": self.generation["environment_fingerprint"],
            "workspace_generation": self.generation["generation"],
            "tested_sha": self.task["tested_sha"],
            "artifact_dir": str(output),
            "log_path": str(log),
            "evidence_exported": True,
            "scope": {"paths": parameters.get("paths", [])},
        }

    def stop_task(self):
        return {"verified": True}

    def stop_codex(self):
        return {"verified": True}

    def stop(self, execution_id):
        return {"verified": True}

    def export_native_evidence(self, destination):
        destination.mkdir(parents=True, exist_ok=True)
        return {"status": "pass"}


class ModelBoundary:
    def __init__(self, hook=None):
        self.calls, self.hook = 0, hook

    def run(self, supervisor, service, recovery=""):
        self.calls += 1
        if self.hook:
            self.hook(supervisor)
        if not supervisor.cancelled.is_set():
            for tool_id in supervisor.policy["required_checks"]:
                record = supervisor.start_check(tool_id, "integration fixture")
                supervisor.poll_check(record["execution_id"], wait_seconds=5)
            supervisor.submit_review("pr_info", "pass", "PR intent is explicit", [])
            supervisor.submit_review(
                "architecture",
                "pass",
                "Documentation preserves contract",
                [{"path": "README.md", "line": 1}],
            )
        result = supervisor.finish("Local integration fixture")
        return {"exit_code": 0, "status": result["status"]}


class UnifiedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        llvm = self.source / "triton/cmake/llvm-hash.txt"
        llvm.parent.mkdir(parents=True)
        llvm.write_text("a" * 40 + "\n")
        version = self.source / "triton/python/triton/__init__.py"
        version.parent.mkdir(parents=True)
        version.write_text('__version__ = "3.0.0"\n')
        (self.source / "README.md").write_text("Base documented behavior\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        base = git(self.source, "rev-parse", "HEAD")
        (self.source / "README.md").write_text("Clarified documented behavior\n")
        git(self.source, "commit", "-qam", "head")
        head = git(self.source, "rev-parse", "HEAD")
        tree = git(self.source, "rev-parse", "HEAD^{tree}")
        tested = git(
            self.source, "commit-tree", tree, "-p", base, "-p", head, input_data=b"merge\n"
        )
        self.remote = self.root / "gitee.git"
        git(self.root, "clone", "--bare", "--quiet", str(self.source), str(self.remote))
        self.task = {
            "schema": TASK_SCHEMA,
            "repository": "likehupochuan/triton-anchor",
            "event_kind": "pull_request",
            "pr_number": 7,
            "tested_sha": tested,
            "base_sha": base,
            "head_sha": head,
            "worker_revision_sha": base,
            "target_branch": "main",
            "title": "Clarify docs",
            "description": "Document existing behavior",
            "labels": [],
            "state": "open",
            "draft": False,
            "captured_at": "2026-09-10T00:00:00Z",
            "llvm_hash": "a" * 40,
            "full": False,
        }
        self.task["metadata_digest"] = metadata_digest(self.task)
        self.task["task_id"] = task_id(self.task)
        prefix = f"ci/pr-7/{self.task['task_id']}"
        self.task.update(
            task_ref=prefix + "/tested",
            base_task_ref=prefix + "/base",
            head_task_ref=prefix + "/head",
        )
        self.attachments = ReleaseBoundary()
        self.relay = GitRelay(
            str(self.remote),
            self.root / "relay",
            allow_local=True,
            attachment_client=self.attachments,
        )
        for field, ref in (
            ("tested_sha", "task_ref"),
            ("base_sha", "base_task_ref"),
            ("head_sha", "head_task_ref"),
        ):
            git(
                self.source,
                "push",
                str(self.remote),
                f"{self.task[field]}:refs/heads/{self.task[ref]}",
            )
        self.relay.write(
            self.relay.control_branch,
            {
                f"tasks/{self.task['task_id']}.json": canonical(self.task),
                f"current/{current_key(self.task)}.json": canonical(
                    {"task_id": self.task["task_id"]}
                ),
            },
        )
        self.relay.refresh()
        self.config = {
            "state_dir": str(self.root / "state"),
            "gitee_repo_url": str(self.remote),
            "simulation": True,
            "minimum_free_bytes": 0,
            "state_min_free_bytes": 0,
            "poll_interval_seconds": 0.02,
            "codex_attempts": 1,
            "retry_delay_seconds": 0,
        }
        self.manager = ContainerBoundary()
        self.driver = ModelBoundary()
        LocalExecutionBoundary.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, driver=None, manager=None):
        return Worker(
            self.config,
            relay=self.relay,
            manager=manager or self.manager,
            driver=driver or self.driver,
            executor_factory=LocalExecutionBoundary,
        )

    def sealed(self, worker):
        directory = worker.journal.run_dir(self.task["task_id"]) / "sealed"
        return directory, json.loads((directory / "result.json").read_bytes())

    def test_sealed_restart_retries_publication_without_new_container_or_model(self):
        self.attachments.offline = True
        first = self.worker()
        first.scan()
        directory, result = self.sealed(first)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(
            first.journal.task(self.task["task_id"])["phase"], "publish_pending"
        )
        self.assertEqual(len(self.manager.destroyed), 1)
        self.assertEqual(len(self.manager.acquired), 1)
        self.assertEqual(self.driver.calls, 1)
        self.assertTrue((directory / result["artifacts"][0]["path"]).is_file())
        validate_execution_summary(
            result, json.loads((directory / "execution-summary.json").read_bytes())
        )
        digest = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
        pending = json.loads((directory.parent / "delivery-index.json").read_bytes())
        self.assertEqual(validate_delivery(result, digest, pending), "pending")
        original = (directory / "result.json").read_bytes()
        self.attachments.offline = False
        second = self.worker()
        second.scan()
        self.assertEqual(
            second.journal.task(self.task["task_id"])["phase"], "published"
        )
        self.assertEqual(self.driver.calls, 1)
        self.assertEqual(len(self.manager.acquired), 1)
        self.assertEqual(len(LocalExecutionBoundary.calls), 1)
        self.assertEqual((directory / "result.json").read_bytes(), original)

    def test_cancelled_current_task_never_seals_a_pass(self):
        def cancel(supervisor):
            self.relay.write(
                self.relay.control_branch,
                {
                    f"cancel/{self.task['task_id']}.json": canonical(
                        {"task_id": self.task["task_id"], "reason": "superseded"}
                    )
                },
            )
            self.assertTrue(
                supervisor.cancelled.wait(5),
                "Worker did not observe Gitee cancellation",
            )

        self.driver = ModelBoundary(cancel)
        worker = self.worker()
        worker.scan()
        _, result = self.sealed(worker)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(LocalExecutionBoundary.calls)

    def test_gitee_outage_pauses_next_execution_stage(self):
        def disconnect(supervisor):
            with patch.object(
                self.relay, "refresh", side_effect=OSError("Gitee unavailable")
            ):
                deadline = time.monotonic() + 5
                while (
                    supervisor.control_available.is_set()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertFalse(supervisor.control_available.is_set())
                with self.assertRaisesRegex(
                    ContractError, "Gitee current state unavailable"
                ):
                    supervisor.start_check(
                        "control_plane", "must wait for control channel"
                    )
                self.assertFalse(LocalExecutionBoundary.calls)
            supervisor.cancel("Stopped integration fixture after outage")

        self.driver = ModelBoundary(disconnect)
        worker = self.worker()
        worker.scan()
        self.assertEqual(self.sealed(worker)[1]["status"], "cancelled")

    def test_preparation_outage_remains_paused_when_supervisor_starts(self):
        outage = patch.object(
            self.relay,
            "refresh",
            side_effect=OSError("Gitee unavailable during preparation"),
        )
        original_acquire = self.manager.acquire_task

        def acquire(*args, **kwargs):
            generation = original_acquire(*args, **kwargs)
            outage.start()
            heartbeat = Path(self.config["state_dir"]) / "health/worker.json"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    heartbeat.exists()
                    and json.loads(heartbeat.read_text()).get("control_channel")
                    == "unreachable"
                ):
                    break
                time.sleep(0.01)
            return generation

        def check(supervisor):
            self.assertFalse(supervisor.control_available.is_set())
            with self.assertRaisesRegex(
                ContractError, "Gitee current state unavailable"
            ):
                supervisor.start_check(
                    "control_plane", "preparation outage must remain visible"
                )
            supervisor.cancel("Stopped fixture after outage")
            outage.stop()

        self.addCleanup(outage.stop)
        self.driver = ModelBoundary(check)
        with patch.object(self.manager, "acquire_task", side_effect=acquire):
            worker = self.worker()
            worker.scan()
        self.assertEqual(self.sealed(worker)[1]["status"], "cancelled")
        self.assertFalse(LocalExecutionBoundary.calls)

    def published_with_optional_pending(self):
        self.config["include_optional_evidence"] = True
        self.attachments.fail_optional = True
        worker = self.worker()
        worker.scan()
        directory, result = self.sealed(worker)
        self.assertEqual(
            worker.journal.task(self.task["task_id"])["phase"], "published"
        )
        index = json.loads((directory.parent / "delivery-index.json").read_bytes())
        self.assertEqual(index["status"], "ready")
        self.assertTrue(
            any(
                not row["required"] and row["status"] == "pending"
                for row in index["artifacts"]
            )
        )
        return worker, directory, result

    def test_optional_retry_finishes_after_publish_without_runtime_or_model(self):
        worker, directory, result = self.published_with_optional_pending()
        original = (directory / "result.json").read_bytes()
        self.attachments.fail_optional = False
        replacement = self.worker()
        with patch.object(
            self.manager, "collect_retired", side_effect=OSError("Docker unavailable")
        ):
            replacement.scan()
        index = json.loads((directory.parent / "delivery-index.json").read_bytes())
        self.assertTrue(all(row["status"] == "ready" for row in index["artifacts"]))
        self.assertEqual(
            replacement.journal.task(self.task["task_id"])["phase"], "published"
        )
        self.assertEqual(self.driver.calls, 1)
        self.assertEqual(len(self.manager.acquired), 1)
        self.assertEqual(len(LocalExecutionBoundary.calls), 1)
        self.assertEqual((directory / "result.json").read_bytes(), original)

    def test_optional_retry_failure_keeps_published_and_observes_cadence(self):
        worker, directory, result = self.published_with_optional_pending()
        published = worker.journal.delivery(self.task["task_id"])["published"]
        with patch.object(
            self.relay, "publish_result", side_effect=OSError("Git push unavailable")
        ) as publish:
            worker.retry_optional_delivery()
            worker.retry_optional_delivery()
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(
            worker.journal.task(self.task["task_id"])["phase"], "published"
        )
        delivery = worker.journal.delivery(self.task["task_id"])
        self.assertEqual(delivery["published"], published)
        self.assertEqual(delivery["optional_attempts"], 1)
        self.assertEqual(self.driver.calls, 1)

    def test_optional_expiry_and_expired_index_stop_retries(self):
        worker, directory, result = self.published_with_optional_pending()
        state = worker.journal._state(self.task["task_id"])
        state["delivery"]["published"] = time.time() - 31 * 86400
        worker.journal._write(self.task["task_id"], state)
        with patch.object(self.relay, "publish_result") as publish:
            worker.retry_optional_delivery()
        publish.assert_not_called()
        state["delivery"]["published"] = time.time()
        worker.journal._write(self.task["task_id"], state)
        index_path = directory.parent / "delivery-index.json"
        index = json.loads(index_path.read_bytes())
        for row in index["artifacts"]:
            if not row["required"]:
                row["status"] = "expired"
        index_path.write_bytes(canonical(index))
        with patch.object(self.relay, "publish_result") as publish:
            worker.retry_optional_delivery()
        publish.assert_not_called()
        self.assertEqual(
            worker.journal.task(self.task["task_id"])["phase"], "published"
        )

    def test_optional_historical_run_retries_without_touching_new_run(self):
        worker, directory, result = self.published_with_optional_pending()
        latest = worker.journal._new(self.task)
        self.attachments.fail_optional = False
        worker.retry_optional_delivery()
        self.assertEqual(
            worker.journal.task(self.task["task_id"])["run_id"], latest["run_id"]
        )
        self.assertEqual(
            worker.journal.task(self.task["task_id"])["phase"], "preparing"
        )
        self.assertEqual(
            json.loads((directory.parent / "state.json").read_bytes())["phase"],
            "published",
        )
        index = json.loads((directory.parent / "delivery-index.json").read_bytes())
        self.assertTrue(all(row["status"] == "ready" for row in index["artifacts"]))
        self.assertEqual(self.driver.calls, 1)


if __name__ == "__main__":
    unittest.main()
