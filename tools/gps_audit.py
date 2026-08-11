#!/usr/bin/env python3
"""Report GPS and timestamp coverage across the live Immich library.

The clusterer's silent failure mode: `segment_trips` keeps only assets where
`has_fix` is true, so a library imported without location data yields zero
trips and no error. This answers "is the metadata actually there?" before you
spend a dry run wondering why nothing came out.

Read-only — it calls the same paginated search the clusterer uses and writes
nothing back.

    python3 tools/gps_audit.py                 # whole library
    python3 tools/gps_audit.py --sample 2000   # stop early, for a quick look
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.config import load_config          # noqa: E402
from tripcluster.geo import haversine_km            # noqa: E402
from tripcluster.immich import ImmichClient, parse_asset  # noqa: E402


def pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:5.1f}%" if total else "    -"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sample", type=int, default=0,
                    help="stop after N assets (0 = whole library)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    client = ImmichClient(cfg.api)
    home = (cfg.home_lat, cfg.home_lon)

    seen = unparsed = with_fix = with_tz = 0
    away = 0
    years: Counter[str] = Counter()
    first = last = None

    for raw in client.iter_assets():
        seen += 1
        a = parse_asset(raw)
        if a is None:
            # parse_asset already logs why; count them so a systematic
            # problem shows up as a number rather than scrolled-past noise.
            unparsed += 1
        else:
            if a.tz_offset_min is not None:
                with_tz += 1
            if a.has_fix:
                with_fix += 1
                if haversine_km(a.lat, a.lon, home[0], home[1]) > cfg.thresholds.home_radius_km:
                    away += 1
            first = a.ts_utc if first is None else min(first, a.ts_utc)
            last = a.ts_utc if last is None else max(last, a.ts_utc)
            years[datetime.fromtimestamp(a.ts_utc, timezone.utc).strftime("%Y")] += 1

        if seen % 2000 == 0:
            print(f"  ...{seen} assets", file=sys.stderr)
        if args.sample and seen >= args.sample:
            break

    if not seen:
        print("No assets returned. Check the API key and that the library is populated.")
        return 1

    parsed = seen - unparsed
    print(f"\nassets examined        {seen}")
    print(f"  parsed               {parsed:7d}  {pct(parsed, seen)}")
    print(f"  unparseable          {unparsed:7d}  {pct(unparsed, seen)}")
    print(f"  with GPS fix         {with_fix:7d}  {pct(with_fix, seen)}   <- the one that matters")
    print(f"  with EXIF tz offset  {with_tz:7d}  {pct(with_tz, seen)}")
    print(f"  away from home       {away:7d}  {pct(away, seen)}   "
          f"(>{cfg.thresholds.home_radius_km:g} km from {home[0]:.4f},{home[1]:.4f})")

    if first and last:
        f = datetime.fromtimestamp(first, timezone.utc).date()
        l = datetime.fromtimestamp(last, timezone.utc).date()
        print(f"\ndate range             {f} .. {l}")
        for y, n in sorted(years.items()):
            print(f"  {y}                 {n:7d}")

    print()
    if with_fix == 0:
        print("STOP: no asset has coordinates. Every trip would be filtered out by")
        print("segment_trips. Check that Location was preserved on import before")
        print("going further.")
        return 1
    share = with_fix / seen
    if share < 0.5:
        print(f"WARNING: only {share:.0%} of assets carry GPS. Trips built from this")
        print("will have sparse stops, and legs will look slower than they were")
        print("because the photo gaps are wider. Worth fixing the import first.")
    elif away == 0:
        print("WARNING: every located photo is inside home_radius_km. Either the")
        print("home coordinates in config.yaml are wrong, or the library genuinely")
        print("has no travel yet. Run tools/find_home.py.")
    else:
        print(f"Looks usable: {share:.0%} located, {away} away-from-home photos to")
        print("cluster. Next: tools/find_home.py, then a dry run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
