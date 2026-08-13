#!/usr/bin/env python3
"""Shared syntax checks for Codex finding locations."""

from __future__ import annotations

import re
from pathlib import PurePosixPath


FINDING_LINE_RE = re.compile(
    r"^(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?$"
)


def parse_finding_line_range(value: str) -> tuple[int, int] | None:
    match = FINDING_LINE_RE.fullmatch(value)
    if match is None:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if end < start:
        return None
    return start, end


def normalized_repository_path(value: str) -> PurePosixPath | None:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        return None
    return path
