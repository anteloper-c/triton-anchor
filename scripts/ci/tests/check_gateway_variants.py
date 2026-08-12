#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


PUBLIC_JOB_BOUNDARY = "\n  validate-dispatch:"


def dispatch_block(text: str) -> str:
    start = text.index("  workflow_dispatch:")
    end = text.index("\npermissions:", start)
    return text[start:end]


def contract_version(text: str) -> str:
    match = re.search(r'^  GATEWAY_CONTRACT_VERSION: "([0-9]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError("gateway contract version is missing")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("router", type=Path)
    parser.add_argument("worker", type=Path)
    args = parser.parse_args()
    router = args.router.read_text(encoding="utf-8").rstrip()
    worker = args.worker.read_text(encoding="utf-8").rstrip()
    public_worker = worker.split(PUBLIC_JOB_BOUNDARY, 1)[0].rstrip()

    if router != public_worker:
        raise SystemExit("Router is not identical to the Worker's public job prefix")
    if dispatch_block(router) != dispatch_block(worker):
        raise SystemExit("Router and Worker workflow_dispatch inputs differ")
    if contract_version(router) != contract_version(worker):
        raise SystemExit("Router and Worker contract versions differ")
    print(f"Gateway variants match Contract v{contract_version(worker)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
