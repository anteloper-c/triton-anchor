#!/usr/bin/env python3
"""Verify the deployable main router matches the pinned control release's route-only prefix."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("router", type=Path)
    parser.add_argument("worker", type=Path)
    args = parser.parse_args()
    router = args.router.read_text().rstrip()
    worker = args.worker.read_text().split("\n  cancel-obsolete:", 1)[0].rstrip()
    if router != worker:
        raise SystemExit(
            "Main router differs from the pinned v4 control release routing prefix"
        )
    print("Gateway v4 router and worker agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
