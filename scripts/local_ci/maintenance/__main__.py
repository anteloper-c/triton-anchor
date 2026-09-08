"""Trusted worker lifecycle CLI (invoke with python -m scripts.local_ci.maintenance)."""
import argparse
import json
from pathlib import Path

from .workers import WorkerBusy, WorkerError, WorkerManager


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--force", action="store_true", help="ignore daily window, never task leases")
    parser.add_argument("action", choices=["ensure", "inspect", "rebuild"])
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    manager = WorkerManager(config["state_dir"], config.get("docker", "docker"))
    failed = False
    profiles = [p for p in config["profiles"] if not args.profile or p["id"] == args.profile]
    if not profiles:
        parser.error("profile not found")
    for profile in profiles:
        try:
            profile = manager.effective_profile(profile)
            result = manager.rebuild(profile, args.force) if args.action == "rebuild" else getattr(manager, args.action)(profile)
        except WorkerBusy as exc:
            result = {"status": "waiting", "error": str(exc)}
        except (WorkerError, ValueError, OSError) as exc:
            result = {"status": "failed", "error": str(exc)}
        print(json.dumps({"profile_id": profile["id"], **result}, ensure_ascii=False))
        failed = failed or result.get("status") == "failed"
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
