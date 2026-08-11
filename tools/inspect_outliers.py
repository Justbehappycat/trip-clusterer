#!/usr/bin/env python3
"""Find photos whose position is impossible given their neighbours, and say what they are.

A leg of 8,000 km in six minutes is not travel and not a GPS error — GPS error
is metres. It is almost always an asset that did not come from your camera: a
saved image, a download, something shared to you, carrying someone else's EXIF
location.

This flags assets that "teleport" — far from the photo before *and* the photo
after — then reports what distinguishes them: camera make and model, MIME type,
and filename shape. If the flagged set is dominated by blank makes or non-camera
types, the fix is to filter by provenance rather than to reject by speed.

Read-only. Filenames are printed as shapes (IMG_####.HEIC), not verbatim.

    python3 tools/inspect_outliers.py
    python3 tools/inspect_outliers.py --min-speed 500
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.config import load_config                      # noqa: E402
from tripcluster.geo import haversine_km                        # noqa: E402
from tripcluster.immich import ImmichClient, ImmichError, parse_asset  # noqa: E402


def shape(name: str) -> str:
    """IMG_4821.HEIC -> IMG_####.HEIC. Reveals the convention, not the file."""
    if not name:
        return "(none)"
    return re.sub(r"\d+", lambda m: "#" * len(m.group()), name)


def describe(raw: dict) -> tuple[str, str, str]:
    exif = raw.get("exifInfo") or {}
    make = (exif.get("make") or "").strip() or "(no make)"
    model = (exif.get("model") or "").strip() or "(no model)"
    return make, model, (raw.get("originalMimeType") or "(none)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--min-speed", type=float, default=1000.0,
                    help="km/h to both neighbours before an asset is an outlier")
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()

    cfg = load_config(args.config)
    try:
        client = ImmichClient(cfg.api)
        raws = list(client.iter_assets())
    except ImmichError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2

    located = []
    for raw in raws:
        a = parse_asset(raw)
        if a is not None and a.has_fix:
            located.append((a, raw))
    located.sort(key=lambda pair: pair[0].ts_utc)
    print(f"{len(raws)} assets, {len(located)} with coordinates\n")

    # Whole-library provenance, for comparison against the flagged set.
    all_make = Counter(describe(r)[0] for _, r in located)
    print("camera make across all located assets:")
    for make, n in all_make.most_common(8):
        print(f"  {n:6d}  {100*n/len(located):5.1f}%  {make}")

    def speed(i: int, j: int) -> float:
        a, b = located[i][0], located[j][0]
        hours = abs(b.ts_utc - a.ts_utc) / 3600.0
        d = haversine_km(a.lat, a.lon, b.lat, b.lon)
        if hours <= 0:
            return float("inf") if d > 1 else 0.0
        return d / hours

    # An outlier is far from what came before AND what came after. A genuine
    # flight is fast in only one direction — you arrive and stay.
    flagged = []
    for i in range(1, len(located) - 1):
        if speed(i - 1, i) > args.min_speed and speed(i, i + 1) > args.min_speed:
            flagged.append(i)

    print(f"\n{len(flagged)} teleporting assets "
          f"(>{args.min_speed:g} km/h to both neighbours)\n")
    if not flagged:
        print("Nothing to explain — the impossible legs are not isolated assets.")
        return 0

    f_make = Counter()
    f_mime = Counter()
    f_shape = Counter()
    for i in flagged:
        raw = located[i][1]
        make, model, mime = describe(raw)
        f_make[f"{make} / {model}"] += 1
        f_mime[mime] += 1
        f_shape[shape(raw.get("originalFileName", ""))] += 1

    print("make / model of the flagged assets:")
    for k, n in f_make.most_common(args.show):
        print(f"  {n:5d}  {k}")
    print("\nMIME type:")
    for k, n in f_mime.most_common(10):
        print(f"  {n:5d}  {k}")
    print("\nfilename shape:")
    for k, n in f_shape.most_common(args.show):
        print(f"  {n:5d}  {k}")

    no_make = sum(n for k, n in f_make.items() if k.startswith("(no make)"))
    print()
    if no_make >= 0.6 * len(flagged):
        print(f"{no_make}/{len(flagged)} flagged assets have no camera make.")
        print("That supports filtering by provenance: these did not come from a")
        print("camera, so they should not contribute stops or legs at all.")
    else:
        print("Most flagged assets do carry camera metadata, so provenance alone")
        print("will not separate them — speed-based rejection is still needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
