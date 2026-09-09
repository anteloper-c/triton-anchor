#!/usr/bin/env python3
"""Local CI control-plane entrypoint."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['poll'])
    args, rest = parser.parse_known_args()
    from runtime.poller import main as poll
    return poll(rest)


if __name__ == '__main__':
    raise SystemExit(main())
