"""
List, restore and hold published registers.

Every publish keeps the outgoing register in build/published/ (the newest few).
Use this when a release turns out to be wrong after it passed the quality gate:
restore the previous register, which also holds the bad vintage so no refresh
publishes it again until its inputs change or the hold is released.

Usage:
    python scripts/restore_register.py --list
    python scripts/restore_register.py --restore <archived file name>
    python scripts/restore_register.py --release-hold

In the compose deployment, run it inside the daemon container, which holds the
build volume:
    docker compose exec daemon uv run python scripts/restore_register.py --list

Check in the Dagster UI that no refresh is running first; the restore refuses
while a run's side database is in build/staging/ unless --force is given (for a
file left behind by a crashed run).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from nfr.orchestration import config, dashboard_cache, publish, registers  # noqa: E402


def _list() -> None:
    live = publish.read_published_fingerprint(str(config.DB))
    print(f"live: {config.DB} (fingerprint {live or 'none'})")
    held = registers.read_hold(config.HOLD_FILE)
    print(f"hold: {held or 'none'}")
    kept = registers.archived(config.ARCHIVE_DIR)
    if not kept:
        print(f"no archived registers in {config.ARCHIVE_DIR}")
        return
    print(f"archived (oldest first, newest {config.ARCHIVE_KEEP} kept):")
    for path in kept:
        print(f"  {path.name}")


def _clear_dashboard_cache() -> str:
    """Empty Superset's chart cache, as a publish does, so the dashboard shows the
    restored register at once. Set in the compose deployment; unset locally."""
    url = os.environ.get(config.DASHBOARD_CACHE_URL_ENV)
    if not url:
        return "dashboard cache not configured here; Superset may show the previous figures until it expires"
    try:
        dashboard_cache.clear_dashboard_cache(url)
    except (OSError, ValueError, dashboard_cache.DashboardCacheError) as exc:
        return f"could not clear the dashboard cache ({exc}); Superset may show the previous figures until it expires"
    return "dashboard cache cleared"


def main() -> int:
    parser = argparse.ArgumentParser(description="List, restore and hold published registers.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="show the live register, the hold and the archive")
    action.add_argument("--restore", metavar="NAME", help="put this archived register back on the live path")
    action.add_argument("--release-hold", action="store_true", help="let the held vintage be published again")
    parser.add_argument("--force", action="store_true",
                        help="restore even though a side database is in build/staging/ (a crashed run)")
    args = parser.parse_args()

    if args.list:
        _list()
        return 0
    if args.release_hold:
        print("hold released" if registers.clear_hold(config.HOLD_FILE) else "no hold to release")
        return 0
    try:
        held = registers.restore(Path(args.restore), config.DB, config.ARCHIVE_DIR, config.HOLD_FILE,
                                 config.ARCHIVE_KEEP, staging_dir=config.STAGING_DIR, force=args.force)
    except registers.RestoreError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"restored {args.restore} onto {config.DB}")
    if held:
        print(f"holding vintage {held}: no refresh will publish it again until its inputs change "
              "or you run --release-hold")
    print(_clear_dashboard_cache())
    return 0


if __name__ == "__main__":
    sys.exit(main())
