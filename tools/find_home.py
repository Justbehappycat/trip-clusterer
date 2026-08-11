#!/usr/bin/env python3
"""Suggest home coordinates from your own library, so §2's inventory isn't a guess.

Home is where you take photos on the most *distinct days* — not where you take
the most photos, which would nominate whichever holiday you shot hardest.

    ./tools/find_home.py --from-json fixtures/synthetic.json
    ./tools/find_home.py --config config.yaml        # uses the cached assets
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.geo import centroid, haversine_km  # noqa: E402
from tripcluster.geocode import label_stops  # noqa: E402
from tripcluster.model import Asset, Stop  # noqa: E402

CELL_DEG = 0.25   # ~25 km latitude cells; coarse on purpose


def load(args) -> list[Asset]:
    if args.from_json:
        from cluster_trips import load_json_assets

        return load_json_assets(Path(args.from_json))

    from tripcluster.config import load_config
    from tripcluster.store import Store

    cfg = load_config(args.config)
    with Store(cfg.state_db) as store:
        return store.all_assets()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--from-json", metavar="FILE")
    p.add_argument("--top", type=int, default=5)
    args = p.parse_args()

    assets = [a for a in load(args) if a.has_fix]
    if not assets:
        print("no assets with coordinates", file=sys.stderr)
        return 1

    cells: dict[tuple[int, int], list[Asset]] = defaultdict(list)
    for a in assets:
        cells[(int(a.lat // CELL_DEG), int(a.lon // CELL_DEG))].append(a)

    ranked = sorted(
        cells.values(),
        key=lambda group: len({a.local_dt.date() for a in group}),
        reverse=True,
    )

    print(f"{len(assets)} placed photos in {len(cells)} cells. Top candidates:\n")

    top = ranked[: args.top]
    stops = []
    for group in top:
        lat, lon = centroid([(a.lat, a.lon) for a in group])
        stops.append(Stop(lat=lat, lon=lon, start_utc=0, end_utc=0))
    try:
        label_stops(stops, "offline")
    except Exception:                       # geocoding is a nicety, not a gate
        pass

    rows = []
    for stop, group in zip(stops, top):
        by_year: Counter[int] = Counter()
        for a in group:
            by_year[a.local_dt.year] += 1
        days = {a.local_dt.date() for a in group}
        rows.append((stop, group, by_year, sorted(days)))

    years = sorted({y for _, _, c, _ in rows for y in c})
    head = "".join(f"{y % 100:>5d}" for y in years)
    print(f"  {'coordinates':>21}  {'days':>5} {'photos':>7}  {head}   place")
    for stop, group, by_year, days in rows:
        spark = "".join(f"{by_year.get(y, 0) or '·':>5}" for y in years)
        print(f"  {stop.lat:9.4f},{stop.lon:10.4f}  {len(days):5d} {len(group):7d}"
              f"  {spark}   {stop.place or ''}")

    print("\nThe per-year columns are the point: a home you moved away from goes")
    print("quiet, and its replacement lights up in the same year. Two rows that")
    print("are both busy across the same years are two homes at once.\n")

    print("Suggested config.yaml block — set `from`/`until` where a row starts or")
    print("stops, and delete any row that is a holiday rather than a home:\n")
    print("homes:")
    for stop, group, by_year, days in rows:
        first, last = days[0], days[-1]
        print(f"  - lat: {stop.lat:.4f}")
        print(f"    lon: {stop.lon:.4f}")
        if stop.place:
            print(f"    label: {stop.place}")
        print(f"    # {len(days)} days, {first} .. {last}")

    lat, lon = rows[0][0].lat, rows[0][0].lon
    away = sum(1 for a in assets if haversine_km(a.lat, a.lon, lat, lon) > 40)
    print(f"\nWith only the top row as home and a 40 km radius, "
          f"{away} of {len(assets)} photos ({away / len(assets):.0%}) count as away.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
