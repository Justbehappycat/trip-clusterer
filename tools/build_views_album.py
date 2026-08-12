#!/usr/bin/env python3
"""Curate a "Views" album for the photo wall: scenery you shot, no people.

The trip albums answer "where did I go". A wall needs a different question:
"is this worth looking at from across the room". This builds one album from
the trip photos, filtered on what the wall actually cares about.

Selection is by metadata, which is exhaustive; smart search only *narrows* it.
That split matters: Immich caps smart search at 40 results per query with no
pagination, so it can never enumerate a library — using it as the selector
yielded 57 photos where the metadata filter finds 722.

Filters, in order of how much they remove from the 2,552 trip photos:

1. **Landscape orientation** (−952). 65% of this library is portrait, and a
   portrait photo on a landscape screen shows pillarboxed against a blurred
   fill. The single biggest lever on how the wall looks.
2. **Stills only** (−706). Live Photo videos and real videos alike.
3. **No detected face** (−172). Only 7% of the library has one, so this is
   nearly free — but it is what keeps portraits of people off the wall.

`--scenic-only` additionally intersects with Immich's CLIP index across
several prompts, unioned because one prompt cannot cover a desert overlook and
a city skyline at once. Much smaller, much more consistent.

Nothing is written without --apply, matching cluster_trips.py.

    python3 tools/build_views_album.py                 # dry run
    python3 tools/build_views_album.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.config import load_config                  # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError     # noqa: E402
from tripcluster.store import Store                          # noqa: E402

# Unioned rather than blended: one prompt cannot cover a desert overlook and a
# city skyline at once, and CLIP similarity is not comparable across prompts.
PROMPTS = [
    "a scenic landscape view",
    "mountains and sky",
    "a coastline or lake",
    "a city skyline",
    "a road stretching into the distance",
    "forest, desert or canyon scenery",
]

ALBUM_NAME = "Views"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--apply", action="store_true",
                    help="write the album; default is a dry run")
    ap.add_argument("--scenic-only", action="store_true",
                    help="also require a match against Immich's scenery search")
    ap.add_argument("--album-name", default=ALBUM_NAME)
    ap.add_argument("--allow-portrait", action="store_true",
                    help="keep portrait photos (they pillarbox on a landscape screen)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        client = ImmichClient(cfg.api)
    except ImmichError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    # Only photos that belong to a trip. A view from the back garden is not
    # what this wall is for.
    with Store(cfg.state_db) as store:
        trip_ids = {
            r[0] for r in store.db.execute("SELECT DISTINCT asset_id FROM trip_assets")
        }
    print(f"{len(trip_ids)} photos belong to a trip")
    if not trip_ids:
        print("No trips recorded. Run cluster_trips.py first.", file=sys.stderr)
        return 1

    def keep(it: dict) -> tuple[bool, str]:
        ex = it.get("exifInfo") or {}
        if (it.get("type") or "IMAGE") != "IMAGE":
            return False, "not a still"
        # No camera make means it did not come out of a camera: screenshots,
        # saved images, downloads. They reach the trip albums legitimately —
        # interpolate_missing_gps gives a screenshot taken mid-trip a position
        # from its neighbours, which is right for "what happened on this trip"
        # and wrong for a wall. 71 of the first 722 were these.
        if not (ex.get("make") or "").strip():
            return False, "not from a camera"
        # An interpolated position is a guess. For the wall we want photos
        # whose location is actually known.
        if ex.get("latitude") is None:
            return False, "no real GPS"
        if it.get("people"):
            return False, "has a face"
        w, h = it.get("width") or 0, it.get("height") or 0
        if not args.allow_portrait and h > w:
            return False, "portrait"
        return True, ""

    chosen, rejected = [], {}
    for raw in client.iter_assets():
        if raw["id"] not in trip_ids:
            continue
        ok, why = keep(raw)
        (chosen.append(raw) if ok else rejected.setdefault(why, []).append(raw))

    print(f"\nof those, after filtering:")
    for why, items in sorted(rejected.items(), key=lambda kv: -len(kv[1])):
        print(f"  -{len(items):5d}  {why}")
    print(f"  ={len(chosen):5d}  landscape stills without faces")

    if args.scenic_only:
        # Smart search caps at 40 per query and cannot paginate, so it can
        # only ever narrow an existing set — never enumerate one.
        scenic: set[str] = set()
        for prompt in PROMPTS:
            try:
                r = client._request("POST", "/api/search/smart",
                                    json={"query": prompt, "size": 250})
            except ImmichError as e:
                print(f"ERROR smart search failed: {e}", file=sys.stderr)
                return 2
            items = (r.get("assets") or {}).get("items") or []
            before = len(scenic)
            scenic.update(i["id"] for i in items)
            print(f"  {prompt:42} {len(items):3d} hits, +{len(scenic)-before} new")
        chosen = [it for it in chosen if it["id"] in scenic]
        print(f"  ={len(chosen):5d}  also matched a scenery prompt")

    if not chosen:
        print("\nNothing selected. Try --allow-portrait, or drop --scenic-only.")
        return 1

    if not args.apply:
        print(f"\nDRY RUN - would put {len(chosen)} photos in \"{args.album_name}\".")
        print("Nothing written. Re-run with --apply.")
        return 0

    ids = [it["id"] for it in chosen]
    existing = next(
        (a for a in client._request("GET", "/api/albums")
         if a.get("albumName") == args.album_name),
        None,
    )
    if existing:
        # Sync, not append. The album is a curated selection, so anything that
        # no longer qualifies has to come out — otherwise a filter fix can
        # never clean up what an earlier run put on the wall.
        current = set()
        page = 1
        while True:
            r = client._request("POST", cfg.api.search_assets, json={
                "albumIds": [existing["id"]], "page": page, "size": 1000,
                "withExif": True, "withPeople": True,
            })
            a = (r.get("assets") or {})
            got = a.get("items") or []
            if not got:
                break
            current.update(i["id"] for i in got)
            nxt = a.get("nextPage")
            if not nxt:
                break
            page = int(nxt)

        want = set(ids)
        stale = sorted(current - want)
        missing = [i for i in ids if i not in current]
        if stale:
            client.remove_assets(existing["id"], stale)
        if missing:
            client.add_assets(existing["id"], missing)
        print(f"\nsynced album {existing['id']}: "
              f"-{len(stale)} removed, +{len(missing)} added, "
              f"{len(want)} on the wall")
    else:
        album_id = client.create_album(
            args.album_name,
            "Scenic photos from trips, curated for the photo wall. "
            "Rebuilt by tools/build_views_album.py.",
            ids,
        )
        print(f"\ncreated album {album_id} with {len(ids)} photos")
    print(f"Point KIOSK_ALBUMS at it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
