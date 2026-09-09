#!/usr/bin/env python3
"""Build and validate one trusted image release; never accepts PR build recipes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environments.manager import EnvironmentManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--reuse", action="store_true",
                        help="Validate current control against matching dependencies without forcing a rebuild")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text())
        branches = [branch for branch, profile in config["profiles"].items() if profile.get("name", branch.replace("/", "-")) == args.profile]
        if len(branches) != 1:
            raise ValueError("Profile name must identify one configured target branch")
        manager = EnvironmentManager(config, config["state_dir"])
        result = (manager.ensure_image(branches[0], config["profiles"][branches[0]]["llvm_hash"])
                  if args.reuse else manager.rotate(branches[0]))
        if not args.reuse:
            result["collection"] = manager.collect_retired()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"Local CI daily environment maintenance failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
