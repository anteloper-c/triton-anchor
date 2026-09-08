"""Real frozen artifact files prove baseline identity and hash selection."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.common import digest, read_json, write_json  # noqa: E402
from runtime.performance import prepare_baselines  # noqa: E402


class PerformanceBaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host_task = self.root / "workspace/tasks/current/run-current"
        self.host_task.mkdir(parents=True)
        self.container_task = "/workspace/tasks/current/run-current"
        self.config = {"state_dir": str(self.root / "state")}
        self.profile = {"id": "triton-3.0", "triton_version": "3.0", "llvm_revision": "a" * 40}
        self.task = {"repository": "anteloper-c/triton-anchor", "base_sha": "b" * 40}

    def historical(self, run_id="run-old", tool="compile_time", **overrides):
        run = self.root / "state/runs/previous" / run_id
        artifact = run / "artifacts" / tool / "candidate.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps({"kernels": {"add": {"median_ms": 1.25}}}))
        result = {"schema": "triton-anchor-local-ci-result/v4", "repository": self.task["repository"],
                  "task_id": "previous", "run_id": run_id, "tested_sha": self.task["base_sha"],
                  "conclusion": "success", "completed_at": "2026-09-08T10:00:00Z",
                  "environment": {"profile_id": self.profile["id"], "llvm_revision": self.profile["llvm_revision"]},
                  "checks": [{"id": tool, "status": "passed", "evidence": ["command-1"]}],
                  "evidence": [{"id": "command-1", "tool": tool, "returncode": 0, "termination": None}],
                  "artifacts": {"files": [{"path": "artifacts/" + tool + "/candidate.json",
                                            "sha256": digest(artifact), "size": artifact.stat().st_size}]}}
        result.update(overrides)
        write_json(run / "result.json", result)
        write_json(run / "execution.json", {"phase": "published"})
        return run, result, artifact

    def prepare(self):
        return prepare_baselines(self.config, self.profile, self.task, self.host_task, self.container_task)

    def test_copies_verified_published_measurement_to_task_baseline(self):
        _, result, artifact = self.historical()
        before = copy.deepcopy(self.profile)
        selected = self.prepare()["compile_time"]
        copied = self.host_task / "baselines/compile_time.json"
        self.assertEqual(copied.read_bytes(), artifact.read_bytes())
        self.assertEqual(selected, {"base_sha": self.task["base_sha"], "profile_id": self.profile["id"],
                                   "llvm_revision": self.profile["llvm_revision"], "sha256": digest(copied),
                                   "path": self.container_task + "/baselines/compile_time.json"})
        artifact.write_text("mutated after the verified snapshot")
        self.assertEqual(digest(copied), selected["sha256"])
        self.assertEqual(self.profile, before)

    def test_wrong_snapshot_hash_is_excluded(self):
        _, _, artifact = self.historical()
        artifact.write_text("{" + " " * (artifact.stat().st_size - 2) + "}")
        self.assertEqual(self.prepare(), {})
        self.assertFalse((self.host_task / "baselines").exists())

    def test_recovery_reuses_existing_snapshot_without_rewriting_it(self):
        self.historical()
        original = self.prepare()
        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError("root-owned read-only baseline")):
            self.assertEqual(self.prepare(), original)
        target = self.host_task / "baselines/compile_time.json"
        target.write_text("changed task baseline")
        with self.assertRaisesRegex(ValueError, "Existing task baseline differs"):
            self.prepare()

    def test_environment_revision_and_profile_mismatch_are_excluded(self):
        run, result, _ = self.historical()
        for environment in ({"profile_id": "other", "llvm_revision": "a" * 40},
                            {"profile_id": self.profile["id"], "llvm_revision": "c" * 40}):
            result["environment"] = environment
            write_json(run / "result.json", result)
            self.assertEqual(self.prepare(), {})

    def test_candidate_claim_cannot_override_host_tested_sha(self):
        run, result, artifact = self.historical(tested_sha="c" * 40)
        artifact.write_text(json.dumps({"tested_sha": self.task["base_sha"], "base_sha": self.task["base_sha"]}))
        result["artifacts"]["files"][0].update(sha256=digest(artifact), size=artifact.stat().st_size)
        write_json(run / "result.json", result)
        self.assertEqual(self.prepare(), {})

    def test_unpublished_failed_or_unproven_results_cannot_be_baselines(self):
        run, result, _ = self.historical()
        write_json(run / "execution.json", {"phase": "publish_pending"})
        self.assertEqual(self.prepare(), {})
        write_json(run / "execution.json", {"phase": "published"})
        result["conclusion"] = "failure"
        write_json(run / "result.json", result)
        self.assertEqual(self.prepare(), {})
        result["conclusion"] = "success"
        result["evidence"][0]["returncode"] = 1
        write_json(run / "result.json", result)
        self.assertEqual(self.prepare(), {})

    def test_explicit_trusted_record_precedes_history_without_host_path_access(self):
        self.historical()
        record = {"base_sha": self.task["base_sha"], "profile_id": self.profile["id"],
                  "llvm_revision": self.profile["llvm_revision"], "sha256": "d" * 64,
                  "path": "/opt/trusted-baselines/base-compile-time.json"}
        self.profile["performance_baselines"] = {"compile_time": record}
        self.assertEqual(self.prepare(), {"compile_time": record})
        self.assertFalse((self.host_task / "baselines").exists())

    def test_mismatched_explicit_record_falls_back_to_verified_history(self):
        self.historical()
        self.profile["performance_baselines"] = {"compile_time": {
            "base_sha": "c" * 40, "profile_id": self.profile["id"],
            "llvm_revision": self.profile["llvm_revision"], "path": "/opt/baseline.json", "sha256": "d" * 64}}
        self.assertEqual(self.prepare()["compile_time"]["base_sha"], self.task["base_sha"])

    def test_matching_explicit_record_rejects_invalid_digest_or_path(self):
        record = {"base_sha": self.task["base_sha"], "profile_id": self.profile["id"],
                  "llvm_revision": self.profile["llvm_revision"], "sha256": "d" * 64, "path": "/opt/baseline.json"}
        self.profile["performance_baselines"] = {"compile_time": record}
        for field, value in (("sha256", "bad-hash"), ("path", "/opt/../baseline.json")):
            original = record[field]
            record[field] = value
            with self.assertRaisesRegex(ValueError, "invalid path or SHA-256"):
                self.prepare()
            record[field] = original

    def test_corrupt_newer_record_does_not_hide_valid_older_baseline(self):
        _, _, artifact = self.historical()
        run, result, bad_artifact = self.historical("run-new", completed_at="2026-09-08T12:00:00Z")
        bad_artifact.write_text("wrong bytes")
        (run.parent / "run-broken").mkdir()
        (run.parent / "run-broken/result.json").write_text("invalid JSON")
        selected = self.prepare()["compile_time"]
        self.assertEqual(selected["sha256"], digest(artifact))

    def test_other_versions_and_missing_history_return_no_baselines(self):
        self.assertEqual(self.prepare(), {})
        self.historical()
        self.profile["triton_version"] = "3.3"
        self.assertEqual(self.prepare(), {})

    def test_duplicate_manifest_entry_is_rejected(self):
        run, result, _ = self.historical()
        result["artifacts"]["files"].append(dict(result["artifacts"]["files"][0]))
        write_json(run / "result.json", result)
        self.assertEqual(self.prepare(), {})


if __name__ == "__main__":
    unittest.main()
