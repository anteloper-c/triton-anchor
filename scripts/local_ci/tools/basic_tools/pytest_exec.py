#!/usr/bin/env python3
"""Invoke pytest with an import-origin observer, for builtin or native execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pytest_origin import ImportOrigin


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installation", default=os.environ.get("LOCAL_CI_INSTALLATION_MANIFEST")
    )
    parser.add_argument(
        "--import-report", default=os.environ.get("LOCAL_CI_IMPORT_REPORT")
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.installation or not args.import_report:
        parser.error("--installation and --import-report are required")
    installation = json.loads(Path(args.installation).read_text())
    arguments = (
        args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    )
    import pytest

    return int(
        pytest.main(arguments, plugins=[ImportOrigin(args.import_report, installation)])
    )


if __name__ == "__main__":
    raise SystemExit(main())
