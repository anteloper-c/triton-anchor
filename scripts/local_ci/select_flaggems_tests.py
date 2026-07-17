#!/usr/bin/env python3
"""Build a FlagGems pytest command for local CI sample/full modes."""

from __future__ import annotations

import argparse
import random
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


IGNORED_MARKERS = {
    "filterwarnings",
    "parametrize",
    "skip",
    "skipif",
    "usefixtures",
    "xfail",
}
MARK_RE = re.compile(r"@pytest\.mark\.([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Entry:
    category: str
    op: str
    marker: str
    test_file: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sample", "full", "single"), default="sample")
    parser.add_argument("--sample-size", type=int, default=6)
    parser.add_argument("--seed", default="")
    parser.add_argument("--op", default="")
    parser.add_argument("--whitelist", required=True)
    parser.add_argument("--full-list", default="")
    parser.add_argument("--flaggems-dir", required=True)
    parser.add_argument("--python-bin", default="python3")
    parser.add_argument("--selected-output", default="")
    return parser.parse_args()


def read_entries(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (3, 4):
            raise ValueError(f"{path}:{line_number}: expected 3 or 4 columns, got {len(parts)}")
        test_file = parts[3] if len(parts) == 4 else ""
        entries.append(Entry(parts[0], parts[1], parts[2], test_file))
    if not entries:
        raise ValueError(f"{path}: no FlagGems operators found")
    return entries


def discover_marker_files(flaggems_dir: Path) -> dict[str, list[str]]:
    tests_dir = flaggems_dir / "tests"
    if not tests_dir.is_dir():
        raise ValueError(f"FlagGems tests directory does not exist: {tests_dir}")

    marker_files: dict[str, list[str]] = {}
    for test_file in sorted(tests_dir.glob("test_*.py")):
        rel_path = test_file.relative_to(flaggems_dir).as_posix()
        text = test_file.read_text(encoding="utf-8", errors="replace")
        for match in MARK_RE.finditer(text):
            marker = match.group(1)
            if marker in IGNORED_MARKERS:
                continue
            marker_files.setdefault(marker, [])
            if rel_path not in marker_files[marker]:
                marker_files[marker].append(rel_path)
    return marker_files


def marker_aliases(entry: Entry) -> list[str]:
    aliases = [entry.marker, entry.op]
    aliases.append(entry.marker.removesuffix("_tensor"))
    aliases.append(entry.marker.removesuffix("_dim"))
    aliases.append(entry.marker.removeprefix("native_"))
    aliases.append(entry.op.removesuffix("_tensor"))
    aliases.append(entry.op.removesuffix("_dim"))
    aliases.append(entry.op.removeprefix("native_"))
    return unique_in_order([alias for alias in aliases if alias])


def attach_discovered_files(entries: list[Entry], marker_files: dict[str, list[str]]) -> list[Entry]:
    resolved: list[Entry] = []
    missing: list[str] = []
    for entry in entries:
        chosen_marker = ""
        files: list[str] = []
        for alias in marker_aliases(entry):
            if alias in marker_files:
                chosen_marker = alias
                files = marker_files[alias]
                break
        if not files:
            missing.append(f"{entry.op}({entry.marker})")
            continue
        for test_file in files:
            resolved.append(Entry(entry.category, entry.op, chosen_marker, test_file))
    if missing:
        raise ValueError("No pytest marker found for full FlagGems ops: " + ", ".join(missing))
    return resolved


def group_entries_by_category(entries: list[Entry]) -> dict[str, list[Entry]]:
    grouped: dict[str, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(entry)
    return grouped


def select_sample_entries(entries: list[Entry], requested_size: int, seed: str) -> list[Entry]:
    sample_size = min(max(requested_size, 1), len(entries))
    rng = random.Random(seed) if seed else random.SystemRandom()
    grouped = group_entries_by_category(entries)
    categories = sorted(grouped)

    if len(categories) <= sample_size:
        selected_categories = categories
    else:
        selected_categories = sorted(rng.sample(categories, sample_size))

    selected: list[Entry] = []
    selected_keys: set[tuple[str, str, str]] = set()
    for category in selected_categories:
        chosen = rng.choice(grouped[category])
        selected.append(chosen)
        selected_keys.add((chosen.op, chosen.marker, chosen.test_file))

    remaining = sample_size - len(selected)
    if remaining > 0:
        pool = [entry for entry in entries if (entry.op, entry.marker, entry.test_file) not in selected_keys]
        selected.extend(rng.sample(pool, remaining))

    return selected


def select_entries(args: argparse.Namespace) -> list[Entry]:
    if args.mode == "full":
        if not args.full_list:
            raise ValueError("--full-list is required in full mode")
        full_entries = read_entries(Path(args.full_list))
        marker_files = discover_marker_files(Path(args.flaggems_dir))
        return attach_discovered_files(full_entries, marker_files)

    entries = read_entries(Path(args.whitelist))
    if args.mode == "single":
        selected = [entry for entry in entries if args.op in (entry.op, entry.marker)]
        if not selected:
            raise ValueError(f"FlagGems op {args.op!r} was not found in the pass whitelist")
        return selected

    return select_sample_entries(entries, args.sample_size, args.seed)


def unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def write_selected(path_text: str, selected: list[Entry], args: argparse.Namespace, command: str) -> None:
    if not path_text:
        return
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique_ops = unique_in_order([entry.op for entry in selected])
    unique_markers = unique_in_order([entry.marker for entry in selected])
    unique_categories = unique_in_order([entry.category for entry in selected])
    lines = [
        f"mode: {args.mode}",
        f"sample_size: {args.sample_size}",
        f"seed: {args.seed}",
        f"selected_op_count: {len(unique_ops)}",
        f"selected_marker_count: {len(unique_markers)}",
        f"selected_category_count: {len(unique_categories)}",
        "selected_ops:",
    ]
    lines.extend(f"- {op}" for op in unique_ops)
    lines.append("selected_categories:")
    lines.extend(f"- {category}" for category in unique_categories)
    lines.append("selected_markers:")
    lines.extend(f"- {marker}" for marker in unique_markers)
    lines.append("selected_entries:")
    lines.extend(
        f"- op: {entry.op}, marker: {entry.marker}, category: {entry.category}, file: {entry.test_file}"
        for entry in selected
    )
    lines.extend(["command:", command])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    selected = select_entries(args)
    test_files = unique_in_order([entry.test_file for entry in selected])
    markers = unique_in_order([entry.marker for entry in selected])
    marker_expr = " or ".join(markers)

    pytest_parts = [
        args.python_bin,
        "-m",
        "pytest",
        "-s",
        *test_files,
        "-m",
        marker_expr,
        "--record=log",
    ]
    command = f"cd {shlex.quote(args.flaggems_dir)} && " + " ".join(shlex.quote(part) for part in pytest_parts)
    write_selected(args.selected_output, selected, args, command)
    print(command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"failed to select FlagGems tests: {exc}", file=sys.stderr)
        raise SystemExit(1)