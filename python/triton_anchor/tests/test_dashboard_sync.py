from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dashboard" / "sync_gitee_results.py"
sys.path.insert(0, str(SCRIPT.parent))
import sync_gitee_results as SYNC  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_run(root: Path, sha: str, run_id: str, status: int) -> Path:
    run = root / "runs" / "ci_push_jiwang-delivery-ci" / sha / run_id
    run.mkdir(parents=True)
    (run / "delivery-summary.txt").write_text(
        "\n".join(
            (
                "schema: triton-anchor-local-ci/v2",
                f"status: {status}",
                f"target_sha: {sha}",
                "branch: ci/push/jiwang-delivery-ci",
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


class DashboardSyncTest(unittest.TestCase):
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
            metric_run = write_run(
                root, metric_sha, "20260720T010000Z-111111111111", 0
            )
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

            SYNC.sync_dashboard(root, output)

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
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
            self.assertEqual(performance["sha"], metric_sha)
            self.assertEqual(performance["compile_time"]["kernels"][0]["candidate_ms"], 12.5)
            self.assertEqual(
                performance["pass_profile"]["hotspots"][0]["name"],
                "add / TritonToLinalg",
            )
            self.assertEqual(performance["ir_serialization"]["metrics"], [])


if __name__ == "__main__":
    unittest.main()
