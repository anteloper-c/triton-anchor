#!/usr/bin/env python3
"""Convert a historical FlagGems CSV into dashboard demo data."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


STATUS_MAP = {
    "成功": "passed",
    "失败": "failed",
    "超时": "timeout",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


def read_text(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("dashboard", payload, 0, 1, "unsupported CSV encoding")


def main() -> int:
    args = parse_args()
    rows = list(csv.DictReader(read_text(args.input_csv).splitlines()))
    operators = []
    for fallback_index, row in enumerate(rows, start=1):
        raw_status = (row.get("测试状态") or "").strip()
        raw_stage = (row.get("最开始失败阶段") or "").strip()
        operators.append(
            {
                "index": int((row.get("序号") or fallback_index)),
                "name": (row.get("算子名称") or f"operator_{fallback_index}").strip(),
                "status": STATUS_MAP.get(raw_status, "unknown"),
                "failure_stage": None if raw_stage == "全部通过" else raw_stage or None,
                "duration_ms": None,
                "tested_at": (row.get("测试时间") or "").strip(),
            }
        )

    document = {
        "schema": "triton-anchor-full-test/v1",
        "data_mode": "mock",
        "source_note": "历史样例，仅用于 GitHub Pages 页面与数据接口验证，不代表当前测试结果。",
        "run": {
            "id": "mock-full-20260720",
            "trigger": "manual",
            "state": "completed",
            "backend": "Sophgo CModel",
            "profile": "sophgo-cmodel",
            "sha": "demo8bae624000000000000000000000000000000",
            "branch": "main",
            "started_at": "2026-07-20T02:00:00Z",
            "finished_at": "2026-07-20T03:12:00Z",
            "result_url": "",
        },
        "operators": operators,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with args.output_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "算子名称", "测试状态", "失败阶段", "耗时(ms)", "测试时间"])
        for row in operators:
            writer.writerow(
                [
                    row["index"],
                    row["name"],
                    row["status"],
                    row["failure_stage"] or "",
                    "",
                    row["tested_at"],
                ]
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
