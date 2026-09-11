"""Direct program loading follows only the pinned control entry."""

import hashlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_ci.program import load_program


def entry(root, text="Trusted program"):
    path = root / "scripts/local_ci/AI_CI_PROGRAM.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_direct_program_ignores_markdown_link_instructions(tmp_path):
    path = entry(tmp_path, "Trusted program [reference](other.md)")
    path.with_name("other.md").write_text("UNREQUESTED INSTRUCTION")
    program = load_program(tmp_path)
    assert program.prompt == path.read_text()
    assert "UNREQUESTED" not in program.prompt
    assert program.manifest == {
        "path": "scripts/local_ci/AI_CI_PROGRAM.md",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_program_content_change_changes_identity(tmp_path):
    path = entry(tmp_path)
    first = load_program(tmp_path).manifest["sha256"]
    path.write_text("Updated trusted program")
    assert load_program(tmp_path).manifest["sha256"] != first


def test_missing_entry_has_no_legacy_fallback(tmp_path):
    legacy = tmp_path / "scripts/local_ci/skills/local-ci/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("Legacy")
    with pytest.raises(FileNotFoundError):
        load_program(tmp_path)


def test_shipped_direct_entry():
    root = Path(__file__).resolve().parents[4]
    program = load_program(root)
    assert program.prompt.strip()
    assert program.manifest["path"] == "scripts/local_ci/AI_CI_PROGRAM.md"
    assert not (root / "scripts/local_ci/skills/local-ci").exists()
