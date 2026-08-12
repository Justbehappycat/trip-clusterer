#!/usr/bin/env python3
"""Cluster an Immich library into trip albums.

Writes nothing unless you pass --apply. Start with a dry run:

    ./cluster_trips.py --config config.yaml
    ./cluster_trips.py --config config.yaml --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from tripcluster.cluster import build_trips
from tripcluster.config import load_config
from tripcluster.geo import drop_impossible_fixes, interpolate_missing_gps
from tripcluster.geocode import label_stops
from tripcluster.model import Asset
from tripcluster.naming import describe, heuristic_name
from tripcluster.store import Store

log = logging.getLogger("cluster_trips")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    if args.check_api:
        return check_api(cfg)

    with Store(cfg.state_db) as store:
        if args.list:
            return list_trips(store)
        if args.rename:
            store.set_custom_name(int(args.rename[0]), args.rename[1] or None)
            log.info("trip %s renamed; it will not be overwritten on rerun", args.rename[0])
            return 0

        assets = load_assets(cfg, store, args)
        if not assets:
            log.error("no assets with usable timestamps - nothing to cluster")
            return 1

        assets.sort(key=lambda a: a.ts_utc)
        # Reject impossible coordinates before anything measures distance:
        # one bad fix mid-trip fabricates two enormous legs and turns a road
        # trip into a flight.
        dropped = drop_impossible_fixes(assets, cfg.thresholds.outlier_min_speed_kmh)
        if dropped:
            log.info("%d impossible GPS fix(es) discarded", dropped)
        with_fix = sum(1 for a in assets if a.has_fix)
        filled = interpolate_missing_gps(
            assets, int(cfg.thresholds.max_interpolation_gap_hours * 3600)
        )
        log.info(
            "%d assets · %d with GPS · %d interpolated · %d still unplaced",
            len(assets), with_fix, filled, len(assets) - with_fix - filled,
        )

        trips = build_trips(assets, cfg.home, cfg.thresholds)
        label_stops([s for t in trips for s in t.stops], cfg.geocoder,
                    cfg.thresholds.major_city_base_km)

        for trip in trips:
            trip.heuristic_name = heuristic_name(trip, cfg.album_prefix)
            store.hydrate(trip)

        if not trips:
            log.warning(
                "no trips matched the thresholds - try lowering min_duration_days "
                "or home_radius_km in %s", args.config
            )
            return 0

        if args.apply:
            return apply_trips(cfg, store, trips)
        print_plan(store, trips)
        return 0


# ---- modes ----------------------------------------------------------------

def load_assets(cfg, store: Store, args) -> list[Asset]:
    if args.from_json:
        return load_json_assets(Path(args.from_json))

    if args.no_fetch:
        cached = store.all_assets()
        log.info("using %d cached assets (--no-fetch)", len(cached))
        return cached

    from tripcluster.immich import ImmichClient, parse_asset

    client = ImmichClient(cfg.api)
    since = None if args.full else store.get_meta("last_fetch_utc")
    if since:
        log.info("fetching assets updated since %s", since)

    fetched, skipped = [], 0
    for raw in client.iter_assets(updated_after=since):
        asset = parse_asset(raw)
        if asset is None:
            skipped += 1
            continue
        fetched.append(asset)

    if skipped:
        log.warning("%d assets skipped (no usable timestamp)", skipped)
    store.upsert_assets(fetched)
    store.set_meta(
        "last_fetch_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    log.info("fetched %d assets from Immich", len(fetched))
    return store.all_assets()


def load_json_assets(path: Path) -> list[Asset]:
    """Load assets from a JSON array — for tuning thresholds without Immich."""
    data = json.loads(path.read_text())
    out = []
    for row in data:
        out.append(
            Asset(
                id=str(row["id"]),
                ts_utc=int(row["ts_utc"]),
                tz_offset_min=row.get("tz_offset_min"),
                lat=row.get("lat"),
                lon=row.get("lon"),
            )
        )
    log.info("loaded %d assets from %s", len(out), path)
    return out


def check_api(cfg) -> int:
    from tripcluster.immich import ImmichClient, ImmichError

    try:
        client = ImmichClient(cfg.api)
    except ImmichError as e:
        log.error("%s", e)
        return 2

    problems = client.verify_spec()
    if not problems:
        print(f"OK - every endpoint the clusterer uses exists on {cfg.api.base_url}")
        return 0
    print(f"Endpoint mismatches against {cfg.api.base_url}:\n")
    for p in problems:
        print(f"  - {p}")
    print("\nFix the paths under `api:` in config.yaml, then re-run --check-api.")
    return 1


def list_trips(store: Store) -> int:
    rows = store.list_trips()
    if not rows:
        print("no trips recorded yet")
        return 0
    for r in rows:
        start = datetime.fromtimestamp(r["start_utc"]).strftime("%Y-%m-%d")
        name = r["custom_name"] or r["heuristic_name"]
        flag = "  (renamed)" if r["custom_name"] else ""
        album = r["album_id"] or "-"
        print(f"[{r['trip_id']:>3}] {start}  {name}{flag}\n      album={album}")
    return 0


def print_plan(store: Store, trips: list) -> None:
    print(f"\nDRY RUN - {len(trips)} trip(s). Nothing written. Re-run with --apply.\n")
    for trip in trips:
        new_ids = store.new_asset_ids(trip)
        if trip.album_id:
            action = (
                f"UPDATE album {trip.album_id} (+{len(new_ids)} new photos)"
                if new_ids else f"UNCHANGED album {trip.album_id}"
            )
        else:
            action = f"CREATE album ({trip.photo_count} photos)"

        print(f"  {trip.display_name}")
        if trip.custom_name:
            print(f"    heuristic name (not applied): {trip.heuristic_name}")
        print(f"    {action}")
        for line in describe(trip).splitlines():
            print(f"    {line}" if line else "")
        print()


def apply_trips(cfg, store: Store, trips: list) -> int:
    from tripcluster.immich import ImmichClient, ImmichError

    client = ImmichClient(cfg.api)
    created = updated = unchanged = 0

    for trip in trips:
        description = describe(trip)
        try:
            if trip.album_id:
                new_ids = store.new_asset_ids(trip)
                if new_ids:
                    client.add_assets(trip.album_id, new_ids)
                    updated += 1
                    log.info("+%d photos -> %s", len(new_ids), trip.display_name)
                else:
                    unchanged += 1
                # The name is only pushed when it was never hand-edited.
                client.update_album(
                    trip.album_id,
                    name=None if trip.custom_name else trip.heuristic_name,
                    description=description,
                )
            else:
                trip.album_id = client.create_album(
                    trip.heuristic_name, description, trip.asset_ids
                )
                created += 1
                log.info("created %s (%d photos)", trip.heuristic_name, trip.photo_count)
        except ImmichError as e:
            log.error("skipping %s: %s", trip.display_name, e)
            continue

        store.save_trip(trip, description)

    log.info("%d created, %d updated, %d unchanged", created, updated, unchanged)
    return 0


# ---- cli ------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--apply", action="store_true",
                   help="actually create/update albums (default is a dry run)")
    p.add_argument("--dry-run", action="store_true",
                   help="explicit no-write run; this is already the default")
    p.add_argument("--full", action="store_true",
                   help="refetch every asset instead of only those updated since last run")
    p.add_argument("--no-fetch", action="store_true",
                   help="cluster the cached assets without calling Immich")
    p.add_argument("--from-json", metavar="FILE",
                   help="cluster assets from a JSON file instead of Immich (threshold tuning)")
    p.add_argument("--check-api", action="store_true",
                   help="verify configured endpoints against the instance's OpenAPI spec")
    p.add_argument("--list", action="store_true", help="list recorded trips and album ids")
    p.add_argument("--rename", nargs=2, metavar=("TRIP_ID", "NAME"),
                   help="set a custom name that reruns will never overwrite")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    if args.apply and args.from_json:
        p.error("--from-json is for tuning only; it cannot be combined with --apply")
    return args


if __name__ == "__main__":
    sys.exit(main())
