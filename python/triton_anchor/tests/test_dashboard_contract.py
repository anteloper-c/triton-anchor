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

    def test_visible_backends_are_configured_without_removing_backend_data(self):
        manifest = read_json(DATA_DIR / "manifest.json")
        document = read_json(DATA_DIR / "backend-status.json")
        backend_ids = {row["id"] for row in document["backends"]}
        visible_ids = manifest["display"]["backend_ids"]
        self.assertEqual(visible_ids, ["sophgo-cmodel"])
        self.assertTrue(set(visible_ids).issubset(backend_ids))

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
        sections = (
            document["compile_time"]["kernels"],
            document["pass_profile"]["hotspots"],
            document["ir_serialization"]["metrics"],
        )
        for rows in sections:
            self.assertIsInstance(rows, list)
        if document.get("data_mode") == "mock":
            self.assertTrue(all(sections))
        else:
            self.assertTrue(any(sections))

    def test_site_entrypoints_exist(self):
        for relative_path in (
            "dashboard/index.html",
            "dashboard/styles.css",
            "dashboard/xlsx.js",
            "dashboard/app.js",
        ):
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)


if __name__ == "__main__":
    unittest.main()
