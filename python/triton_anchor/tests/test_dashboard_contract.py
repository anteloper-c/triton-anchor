from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "dashboard" / "data"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class DashboardContractTest(unittest.TestCase):
    def test_manifest_sources_exist(self):
        manifest = read_json(DATA_DIR / "manifest.json")
        self.assertEqual(manifest["schema"], "triton-anchor-dashboard-manifest/v1")
        for relative_path in manifest["sources"].values():
            self.assertTrue((DATA_DIR / relative_path).is_file(), relative_path)
        for relative_path in manifest["downloads"].values():
            self.assertTrue((DATA_DIR / relative_path).is_file(), relative_path)

    def test_full_test_operators_have_stable_shape(self):
        document = read_json(DATA_DIR / "full-test.json")
        self.assertEqual(document["schema"], "triton-anchor-full-test/v1")
        self.assertGreaterEqual(len(document["operators"]), 100)
        names = set()
        for row in document["operators"]:
            self.assertIn(row["status"], {"passed", "failed", "timeout", "unknown"})
            self.assertTrue(row["name"])
            self.assertNotIn(row["name"], names)
            names.add(row["name"])

    def test_backend_statuses_are_unique(self):
        document = read_json(DATA_DIR / "backend-status.json")
        self.assertEqual(document["schema"], "triton-anchor-backend-status-list/v1")
        backend_ids = [row["id"] for row in document["backends"]]
        self.assertEqual(len(backend_ids), len(set(backend_ids)))
        self.assertIn("sophgo-cmodel", backend_ids)
        for row in document["backends"]:
            self.assertIn(
                row["state"],
                {"success", "warning", "failure", "pending", "stale", "unknown"},
            )

    def test_performance_contract_contains_required_sections(self):
        document = read_json(DATA_DIR / "performance.json")
        self.assertEqual(document["schema"], "triton-anchor-performance-summary/v1")
        self.assertTrue(document["compile_time"]["kernels"])
        self.assertTrue(document["pass_profile"]["hotspots"])
        self.assertTrue(document["ir_serialization"]["metrics"])

    def test_site_entrypoints_exist(self):
        for relative_path in ("dashboard/index.html", "dashboard/styles.css", "dashboard/app.js"):
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)


if __name__ == "__main__":
    unittest.main()
