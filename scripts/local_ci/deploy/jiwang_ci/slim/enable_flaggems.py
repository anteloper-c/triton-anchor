"""Expose a trusted read-only FlagGems source mount without installing its wheel."""
import argparse
from pathlib import Path
import sysconfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    source = Path(args.source)
    root = Path("/opt/local-ci/runtime/deps")
    if source.parent != root or not source.name or any(char in args.source for char in "\r\n"):
        raise ValueError("FlagGems must be a direct, trusted dependency mount")
    # The mount is deliberately absent during image build. No package is imported.
    path = Path(sysconfig.get_path("purelib")) / "local_ci_flaggems.pth"
    path.write_text(str(source / "src") + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
