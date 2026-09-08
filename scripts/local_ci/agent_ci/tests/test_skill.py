"""Trusted Skill loading, path boundaries and version identity; no model calls."""
from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.protocol import ContractError
from agent_ci.skill import MAX_FILE_BYTES, load_skill


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "references").mkdir()
        (self.root / "references/program.md").write_text("Current trusted program", encoding="utf-8")
        (self.root / "references/rules.md").write_text("Current trusted rules", encoding="utf-8")
        self.entry = self.root / "SKILL.md"
        self.entry.write_text("---\nname: local-ci\ndescription: CI fixture\n---\n"
                              "[Program](references/program.md)\n[Rules](references/rules.md)\n", encoding="utf-8")

    def test_entry_controls_order_and_ignores_unreferenced_or_legacy_prompts(self):
        for path in (self.root / "ai_ci_program.md", self.root / "architecture_review.md",
                     self.root / "ai_review.md", self.root / "README.md", self.root / "references/unused.md"):
            path.write_text("MUST NEVER ENTER PROMPT", encoding="utf-8")
        bundle = load_skill(self.root)
        self.assertEqual(["SKILL.md", "references/program.md", "references/rules.md"],
                         [file["path"] for file in bundle.manifest["files"]])
        self.assertNotIn("MUST NEVER ENTER PROMPT", bundle.prompt)
        self.assertLess(bundle.prompt.index("Current trusted program"), bundle.prompt.index("Current trusted rules"))
        before = bundle.manifest["digest"]
        (self.root / "references/rules.md").write_text("Changed trusted rules", encoding="utf-8")
        self.assertNotEqual(before, load_skill(self.root).manifest["digest"])

    def test_missing_entry_or_reference_fails_without_legacy_fallback(self):
        (self.root / "ai_ci_program.md").write_text("Old instructions", encoding="utf-8")
        (self.root / "references/program.md").unlink()
        with self.assertRaisesRegex(ContractError, "references/program.md"):
            load_skill(self.root)
        self.entry.unlink()
        with self.assertRaisesRegex(ContractError, "SKILL.md"):
            load_skill(self.root)

    def test_invalid_reference_paths_are_rejected(self):
        for target in ("../secret.md", "/tmp/secret.md", "references/../../secret.md",
                       "references/./rules.md", "references//rules.md", "references\\rules.md",
                       "https://example.invalid/rules.md", "references/rules.md#section"):
            with self.subTest(target=target):
                self.entry.write_text(f"[Invalid]({target})", encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "relative references"):
                    load_skill(self.root)

    def test_invalid_resources_and_duplicates_fail_closed(self):
        source = (self.root / "references/program.md")
        for content in ("", " " * (MAX_FILE_BYTES + 1)):
            source.write_text(content, encoding="utf-8")
            with self.assertRaises(ContractError):
                load_skill(self.root)
        self.entry.write_text("[One](references/rules.md) [Two](references/rules.md)", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "Duplicate"):
            load_skill(self.root)
        self.entry.write_text("Entry without references", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "reference links"):
            load_skill(self.root)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux deployed filesystem")
    def test_symlink_resource_is_rejected_even_inside_skill(self):
        path = self.root / "references/program.md"
        path.unlink()
        path.symlink_to(self.root / "references/rules.md")
        with self.assertRaisesRegex(ContractError, "symlink"):
            load_skill(self.root)

    def test_shipped_skill_has_all_required_resources_and_no_flat_entry(self):
        bundle = load_skill()
        self.assertEqual(["SKILL.md", "references/AI_CI_PROGRAM.md", "references/architecture_review.md",
                          "references/ai_review.md", "references/project_conventions.md"],
                         [file["path"] for file in bundle.manifest["files"]])
        local_root = Path(__file__).resolve().parents[2]
        for name in ("ai_ci_program.md", "architecture_review.md", "ai_review.md"):
            self.assertFalse((local_root / name).exists())


if __name__ == "__main__":
    unittest.main()
