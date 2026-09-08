"""Load the trusted local-ci Skill explicitly, independently of CLI discovery."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .protocol import ContractError


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills/local-ci"
LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
MAX_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class SkillBundle:
    prompt: str
    manifest: dict


def load_skill(root: Path = SKILL_ROOT) -> SkillBundle:
    """SKILL.md is the only entry; its ordered references are the load manifest.

    This package uses simple relative Markdown links under references/. Never
    discover sibling prompts, recurse through PR files, or fall back to legacy
    prompts when an entry/reference is invalid.
    """
    root = Path(root).absolute()

    def read(relative: str) -> str:
        path = root / relative
        try:
            if path.resolve(strict=True) != path or not path.is_file():
                raise ContractError(f"Skill file must be regular without symlink components: {relative}")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ContractError(f"Skill file exceeds size limit: {relative}")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, RuntimeError) as exc:
            raise ContractError(f"Cannot load trusted Skill file {relative}: {exc}") from exc
        if not content.strip():
            raise ContractError(f"Skill file is empty: {relative}")
        return content

    entry = read("SKILL.md")
    references = []
    for target in LINK.findall(entry):
        path = PurePosixPath(target)
        if (not target.startswith("references/") or path.suffix != ".md"
                or ".." in path.parts or "\\" in target or path.as_posix() != target):
            raise ContractError(f"Skill reference must be a relative references/*.md path: {target}")
        if target in references:
            raise ContractError(f"Duplicate Skill reference: {target}")
        references.append(target)
    if not references or len(references) > 16:
        raise ContractError("Skill entry must declare 1–16 reference links")
    sources = [("SKILL.md", entry)] + [(path, read(path)) for path in references]
    files = [{"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest()} for path, content in sources]
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"schema": "local-ci-skill/v1", "name": "local-ci", "entrypoint": "SKILL.md",
                "digest": digest, "files": files}
    prompt = "\n\n".join(f"--- BEGIN TRUSTED SKILL FILE: {path} ---\n{content.rstrip()}\n--- END TRUSTED SKILL FILE: {path} ---"
                         for path, content in sources)
    return SkillBundle(prompt=prompt, manifest=manifest)
