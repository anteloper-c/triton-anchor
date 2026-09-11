"""Load the single Agent entry from the frozen control checkout."""

from dataclasses import dataclass
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class Program:
    prompt: str
    manifest: dict


def load_program(control_root: Path) -> Program:
    path = Path(control_root) / "scripts/local_ci/AI_CI_PROGRAM.md"
    data = path.read_bytes()
    return Program(
        data.decode("utf-8"),
        {
            "path": "scripts/local_ci/AI_CI_PROGRAM.md",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
