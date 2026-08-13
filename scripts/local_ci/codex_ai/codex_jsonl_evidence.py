#!/usr/bin/env python3
"""Timestamp Codex JSONL events and extract trusted command execution facts."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any


TIMESTAMP_KEY = "_runner_recorded_at_seconds"


def normalize_command(value: Any) -> str | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = value
        text = shlex.join(parts)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = []
    else:
        return None

    if len(parts) >= 3:
        shell_index = next(
            (
                index
                for index, part in enumerate(parts[:-1])
                if Path(part).name in {"bash", "sh", "zsh"}
                and parts[index + 1] == "-lc"
            ),
            -1,
        )
        if shell_index >= 0 and shell_index + 2 < len(parts):
            return parts[shell_index + 2].strip()
    return text


def record() -> int:
    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line, flush=True)
            continue
        if isinstance(event, dict):
            event[TIMESTAMP_KEY] = round(time.monotonic(), 6)
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
        else:
            print(line, flush=True)
    return 0


def extract(input_path: Path, output_path: Path) -> int:
    started: dict[str, float] = {}
    ledger: list[dict[str, Any]] = []
    with input_path.open(encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            item_id = item.get("id")
            recorded_at = event.get(TIMESTAMP_KEY)
            if (
                event_type == "item.started"
                and isinstance(item_id, str)
                and isinstance(recorded_at, (int, float))
            ):
                started[item_id] = float(recorded_at)
                continue
            if event_type != "item.completed":
                continue
            exit_code = item.get("exit_code")
            command = normalize_command(item.get("command"))
            if not isinstance(exit_code, int) or isinstance(exit_code, bool) or command is None:
                continue
            duration_value = item.get("duration_seconds")
            if (
                isinstance(duration_value, (int, float))
                and not isinstance(duration_value, bool)
                and duration_value >= 0
            ):
                duration = float(duration_value)
            elif (
                isinstance(item_id, str)
                and item_id in started
                and isinstance(recorded_at, (int, float))
            ):
                duration = max(0.0, float(recorded_at) - started[item_id])
            else:
                duration = 0.0
            ledger.append(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "duration_seconds": round(duration, 3),
                }
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def has_event(input_path: Path, expected_type: str) -> bool:
    with input_path.open(encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == expected_type:
                return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("record")
    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--input", type=Path, required=True)
    extract_parser.add_argument("--output", type=Path, required=True)
    event_parser = subparsers.add_parser("has-event")
    event_parser.add_argument("--input", type=Path, required=True)
    event_parser.add_argument("--type", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "record":
        return record()
    if args.command == "has-event":
        return 0 if has_event(args.input, args.type) else 1
    return extract(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
