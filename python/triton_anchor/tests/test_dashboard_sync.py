from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dashboard" / "sync_gitee_results.py"
sys.path.insert(0, str(SCRIPT.parent))
import sync_gitee_results as SYNC  # noqa: E402

PUSH_SOURCE = "ci/push/release-v3"
FULL_SOURCE = "ci/full/release-v3"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_run(root: Path, sha: str, run_id: str, status: int) -> Path:
    run = root / SYNC.result_task_dir(PUSH_SOURCE) / sha / run_id
    run.mkdir(parents=True)
    (run / "delivery-summary.txt").write_text(
        "\n".join(
            (
                "schema: triton-anchor-local-ci/v2",
                f"status: {status}",
                f"target_sha: {sha}",
                f"branch: {PUSH_SOURCE}",
                "backend_profile: sophgo-cmodel",
                "compile_time_status: pass",
                "pass_profile_status: pass",
                "ir_serialization_status: disabled",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return run


def write_full_run(
    root: Path,
    sha: str,
    run_id: str,
    branch: str = FULL_SOURCE,
) -> Path:
    run = root / SYNC.result_task_dir(branch) / sha / run_id
    run.mkdir(parents=True)
    (run / "delivery-summary.txt").write_text(
        "\n".join(
            (
                "schema: triton-anchor-local-ci/v2",
                "status: 1",
                f"target_sha: {sha}",
                f"branch: {branch}",
                "backend_profile: sophgo-cmodel",
                "flaggems_status: fail",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        run / "flaggems-summary.json",
        {
            "schema": "triton-anchor-local-ci/flaggems-v1",
            "mode": "full",
            "summary": {
                "total": 3,
                "passed": 1,
                "failed": 1,
                "timed_out": 1,
                "status": "fail",
            },
            "results": [
                {
                    "index": 1,
                    "op": "add",
                    "test_status": "success",
                    "first_failed_stage": "all passed",
                    "started_at": "10:00:00",
                    "duration_seconds": 1.25,
                    "exit_code": 0,
                    "timeout_reason": "",
                },
                {
                    "index": 2,
                    "op": "dropout",
                    "test_status": "failed",
                    "first_failed_stage": "Linalg generation",
                    "started_at": "10:01:00",
                    "duration_seconds": 2.5,
                    "exit_code": -6,
                    "timeout_reason": "",
                },
                {
                    "index": 3,
                    "op": "erf",
                    "test_status": "timeout",
                    "first_failed_stage": "timeout",
                    "started_at": "10:02:00",
                    "duration_seconds": 3.75,
                    "exit_code": -9,
                    "timeout_reason": "idle",
                },
            ],
        },
    )
    return run


class DashboardSyncTest(unittest.TestCase):
    def test_cli_requires_explicit_push_and_full_sources(self):
        with mock.patch.object(
            sys,
            "argv",
            [
                "sync_gitee_results.py",
                "--results-dir",
                "/tmp/results",
                "--output-dir",
                "/tmp/output",
            ],
        ):
            with self.assertRaises(SystemExit):
                SYNC.parse_args()

    def test_result_paths_use_canonical_task_groups(self):
        self.assertEqual(
            SYNC.result_task_dir(FULL_SOURCE).as_posix(),
            "runs/ci_full/ci_full_release-v3",
        )
        self.assertEqual(
            SYNC.result_task_dir("ci/full/release-candidate").as_posix(),
            "runs/ci_full/ci_full_release-candidate",
        )
        self.assertEqual(
            SYNC.result_task_dir(PUSH_SOURCE).as_posix(),
            "runs/ci_push/ci_push_release-v3",
        )
        self.assertEqual(
            SYNC.result_task_dir("ci/pr-9/feat/backend-status-pages").as_posix(),
            "runs/ci_pr/ci_pr-9_feat_backend-status-pages",
        )
        self.assertEqual(
            SYNC.result_task_dir("ci/base/pr-9/feat/backend-status-pages").as_posix(),
            "runs/ci_pr/ci_base_pr-9_feat_backend-status-pages",
        )

    def test_full_runs_are_isolated_by_task_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sha = "a" * 40
            write_full_run(
                root,
                sha,
                "20260723T030000Z-aaaaaaaaaaaa",
                branch="ci/full/release-candidate",
            )

            self.assertEqual(SYNC.discover_runs(root, FULL_SOURCE), [])
            runs = SYNC.discover_runs(root, "ci/full/release-candidate")

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].source_branch, "ci/full/release-candidate")
            self.assertIn(
                "/runs/ci_full/ci_full_release-candidate/",
                SYNC.result_url(
                    runs[0],
                    "https://gitee.example/results",
                    "local-ci-results",
                ),
            )

    def test_latest_run_sets_health_and_latest_metrics_remain_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "dashboard-data"
            output.mkdir()
            write_json(
                output / "manifest.json",
                {
                    "schema": "triton-anchor-dashboard-manifest/v1",
                    "mode": "mock",
                    "sources": {
                        "full_test": "full-test.json",
                        "backend_status": "backend-status.json",
                        "performance": "performance.json",
                    },
                    "downloads": {"full_test_csv": "full-test.csv"},
                },
            )

            metric_sha = "1" * 40
            metric_run = write_run(root, metric_sha, "20260720T010000Z-111111111111", 0)
            write_json(
                metric_run / "compile-benchmark.json",
                {
                    "metadata": {
                        "backend_profile": "sophgo-cmodel",
                        "kernels": ["add"],
                    },
                    "summary": {
                        "add": {
                            "all_correct": True,
                            "compile_est": {"median_ms": 12.5},
                        }
                    },
                },
            )
            write_json(
                metric_run / "pass-profile.json",
                {
                    "metadata": {"backend_profile": "sophgo-cmodel"},
                    "summary": {
                        "add": {
                            "hotspots": [
                                {"name": "Total", "median_ms": 8.0},
                                {"name": "TritonToLinalg", "median_ms": 4.0},
                            ]
                        }
                    },
                },
            )

            failed_sha = "2" * 40
            write_run(root, failed_sha, "20260721T020000Z-222222222222", 1)

            SYNC.sync_dashboard(root, output, PUSH_SOURCE, FULL_SOURCE)

            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            backend = json.loads(
                (output / "backend-status.json").read_text(encoding="utf-8")
            )
            performance = json.loads(
                (output / "performance.json").read_text(encoding="utf-8")
            )

            self.assertEqual(manifest["mode"], "mixed")
            self.assertEqual(manifest["data_modes"]["full_test"], "mock")
            self.assertEqual(backend["backends"][0]["state"], "failure")
            self.assertEqual(backend["backends"][0]["sha"], failed_sha)
            self.assertIn(
                "/runs/ci_push/ci_push_release-v3/",
                backend["backends"][0]["result_url"],
            )
            self.assertEqual(performance["sha"], metric_sha)
            self.assertEqual(
                performance["compile_time"]["kernels"][0]["candidate_ms"], 12.5
            )
            self.assertEqual(
                performance["pass_profile"]["hotspots"][0]["name"],
                "add / TritonToLinalg",
            )
            self.assertEqual(performance["ir_serialization"]["metrics"], [])

    def test_full_test_uses_manual_run_without_replacing_mainline_performance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "dashboard-data"
            output.mkdir()
            write_json(
                output / "manifest.json",
                {
                    "schema": "triton-anchor-dashboard-manifest/v1",
                    "mode": "mock",
                    "sources": {
                        "full_test": "full-test.json",
                        "backend_status": "backend-status.json",
                        "performance": "performance.json",
                    },
                    "downloads": {"full_test_csv": "full-test.csv"},
                },
            )

            main_sha = "3" * 40
            main_run = write_run(root, main_sha, "20260722T010000Z-333333333333", 0)
            write_json(
                main_run / "compile-benchmark.json",
                {
                    "metadata": {"kernels": ["add"]},
                    "summary": {
                        "add": {
                            "all_correct": True,
                            "compile_est": {"median_ms": 8.5},
                        }
                    },
                },
            )

            pr_sha = "5" * 40
            pr_run = (
                root
                / "runs"
                / "ci_pr"
                / "ci_pr-9_feature"
                / pr_sha
                / "20260723T015000Z-555555555555"
            )
            pr_run.mkdir(parents=True)
            write_json(
                pr_run / "compile-benchmark.json",
                {
                    "metadata": {"kernels": ["add"]},
                    "summary": {
                        "add": {
                            "all_correct": True,
                            "compile_est": {"median_ms": 99.0},
                        }
                    },
                },
            )

            full_sha = "4" * 40
            write_full_run(root, full_sha, "20260723T020000Z-444444444444")

            SYNC.sync_dashboard(root, output, PUSH_SOURCE, FULL_SOURCE)

            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            backend = json.loads(
                (output / "backend-status.json").read_text(encoding="utf-8")
            )
            performance = json.loads(
                (output / "performance.json").read_text(encoding="utf-8")
            )
            full_test = json.loads(
                (output / "full-test.json").read_text(encoding="utf-8")
            )
            full_csv = (output / "full-test.csv").read_bytes()

            self.assertEqual(manifest["mode"], "live")
            self.assertEqual(manifest["data_modes"]["full_test"], "live")
            self.assertEqual(backend["backends"][0]["sha"], main_sha)
            self.assertEqual(performance["sha"], main_sha)
            self.assertEqual(full_test["run"]["sha"], full_sha)
            self.assertEqual(full_test["run"]["branch"], FULL_SOURCE)
            self.assertEqual(
                [row["status"] for row in full_test["operators"]],
                ["passed", "failed", "timeout"],
            )
            self.assertEqual(full_test["operators"][0]["duration_ms"], 1250.0)
            self.assertTrue(full_csv.startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
