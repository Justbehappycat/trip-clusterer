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
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.geo import centroid, haversine_km  # noqa: E402
from tripcluster.model import Asset  # noqa: E402

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
    top_centroid = None
    for group in ranked[: args.top]:
        lat, lon = centroid([(a.lat, a.lon) for a in group])
        days = len({a.local_dt.date() for a in group})
        if top_centroid is None:
            top_centroid = (lat, lon)
        print(f"  {lat:9.4f}, {lon:10.4f}   {days:4d} distinct days, "
              f"{len(group):5d} photos")

    lat, lon = top_centroid
    away = sum(1 for a in assets if haversine_km(a.lat, a.lon, lat, lon) > 40)
    print(f"\nWith home = {lat:.4f}, {lon:.4f} and a 40 km radius, "
          f"{away} of {len(assets)} photos ({away / len(assets):.0%}) count as away.")
    print("\nPaste the top row into config.yaml under `home:`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
