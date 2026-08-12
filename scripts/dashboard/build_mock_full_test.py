#!/usr/bin/env python3
"""Convert a historical FlagGems CSV into dashboard demo data."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


STATUS_MAP = {
    "通过": "passed",
    "成功": "passed",
    "失败": "failed",
    "超时": "timeout",
}

STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "timeout": "超时",
    "unknown": "未知",
}

DEFAULT_DEMO_SHA = "demo3d4c586307dcc3c1f11e650c67529b85da3dd22f"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--run-id", default="mock-full-20260724-3d4c5863")
    parser.add_argument("--sha", default=DEFAULT_DEMO_SHA)
    parser.add_argument("--branch", default="ci/full/jiwang-delivery-ci")
    parser.add_argument("--started-at", default="2026-07-24T11:24:10Z")
    parser.add_argument("--finished-at", default="")
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
        raw_stage = (row.get("失败阶段") or row.get("最开始失败阶段") or "").strip()
        status = STATUS_MAP.get(raw_status, "unknown")
        raw_duration = (row.get("耗时(ms)") or "").strip()
        duration_ms = float(raw_duration) if raw_duration else None
        operators.append(
            {
                "index": int((row.get("序号") or fallback_index)),
                "name": (row.get("算子名称") or f"operator_{fallback_index}").strip(),
                "status": status,
                "failure_stage": (
                    None
                    if status == "passed" or raw_stage == "全部通过"
                    else raw_stage or None
                ),
                "duration_ms": duration_ms,
                "tested_at": (row.get("测试时间") or "").strip(),
            }
        )

    summary = {
        "total": len(operators),
        "passed": sum(1 for row in operators if row["status"] == "passed"),
        "failed": sum(1 for row in operators if row["status"] == "failed"),
        "timed_out": sum(1 for row in operators if row["status"] == "timeout"),
    }
    summary["status"] = "pass" if summary["failed"] == 0 and summary["timed_out"] == 0 else "fail"

    document = {
        "schema": "triton-anchor-full-test/v1",
        "data_mode": "mock",
        "source_note": "历史样例，来自 2026-07-24 手动全量算子测试；仅用于无 live 全量算子结果时的页面展示。",
        "source_summary": summary,
        "run": {
            "id": args.run_id,
            "trigger": "manual",
            "state": "completed",
            "backend": "Sophgo CModel",
            "profile": "sophgo-cmodel",
            "sha": args.sha,
            "branch": args.branch,
            "started_at": args.started_at,
            "finished_at": args.finished_at,
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
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["序号", "算子名称", "测试状态", "失败阶段", "耗时(ms)", "测试时间"])
        for row in operators:
            writer.writerow(
                [
                    row["index"],
                    row["name"],
                    STATUS_LABELS.get(row["status"], row["status"]),
                    row["failure_stage"] or "",
                    row["duration_ms"] if row["duration_ms"] is not None else "",
                    row["tested_at"],
                ]
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
