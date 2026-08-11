#!/usr/bin/env python3
"""Report the *shape* of what the search endpoint returns — never the values.

`parse_asset` looks for coordinates only under `exifInfo`. If a release stops
returning that block from the search endpoint, every asset parses fine, keeps
its timestamp, and silently loses its GPS — which looks identical to a library
imported without location. This tells the two apart.

Prints key names, types and counts only. No coordinates, filenames, or
timestamps are printed, so the output is safe to paste into a chat or an issue.

    python3 tools/inspect_asset_shape.py
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.config import load_config      # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError  # noqa: E402


def typename(v) -> str:
    if v is None:
        return "null"
    return type(v).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--pages", type=int, default=2, help="how many pages to walk")
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        client = ImmichClient(cfg.api)
    except ImmichError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    print(f"search endpoint : {cfg.api.search_assets}")
    print(f"page_size       : {cfg.api.page_size}")

    total = 0
    for page in range(1, args.pages + 1):
        body = {"page": page, "size": cfg.api.page_size}
        try:
            data = client._request("POST", cfg.api.search_assets, json=body)
        except ImmichError as e:
            print(f"ERROR {e}", file=sys.stderr)
            return 2

        # Envelope shape: where do items and the next-page cursor actually live?
        if isinstance(data, dict):
            print(f"\npage {page} envelope keys: {sorted(data.keys())}")
            for k, v in data.items():
                if isinstance(v, dict):
                    # count/total are the answer to "is this the whole
                    # library or just the first page?", so show their values.
                    nums = {n: v[n] for n in ("count", "total", "nextPage")
                            if n in v}
                    print(f"  {k}: dict{sorted(v.keys())}  {nums}")
                elif isinstance(v, list):
                    print(f"  {k}: list[{len(v)}]")
                else:
                    print(f"  {k}: {typename(v)} = {v!r}")

        items = None
        if isinstance(data, dict):
            items = (data.get("assets") or {}).get("items") if isinstance(
                data.get("assets"), dict) else data.get("items")
        if not items:
            print(f"page {page}: no items — pagination ends here")
            break
        total += len(items)

        if page == 1:
            first = items[0]
            print(f"\nasset keys ({len(first)}): {sorted(first.keys())}")
            exif = first.get("exifInfo")
            if exif is None:
                print("\n  *** exifInfo is ABSENT from search results ***")
                print("      parse_asset reads latitude/longitude only from")
                print("      exifInfo, so every asset will come back without a")
                print("      GPS fix regardless of what the photo contains.")
            else:
                print(f"\n  exifInfo keys ({len(exif)}): {sorted(exif.keys())}")
                for k in ("latitude", "longitude", "dateTimeOriginal", "timeZone"):
                    present = k in exif and exif[k] is not None
                    print(f"    {k:18} {'present' if present else 'MISSING/null'}"
                          f"  ({typename(exif.get(k))})")

            # Any top-level key that smells like a coordinate.
            cand = [k for k in first if any(
                s in k.lower() for s in ("lat", "lon", "lng", "gps", "coord", "exif"))]
            print(f"\n  coordinate-ish top-level keys: {cand or 'none'}")

        # How many in this page carry usable coordinates, counted not printed.
        c = Counter()
        for it in items:
            ex = it.get("exifInfo") or {}
            c["has_exifInfo"] += 1 if it.get("exifInfo") else 0
            c["has_latlon"] += 1 if ex.get("latitude") is not None and \
                ex.get("longitude") is not None else 0
        print(f"\npage {page}: {len(items)} items, "
              f"{c['has_exifInfo']} with exifInfo, {c['has_latlon']} with lat+lon")

    print(f"\ntotal walked: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
