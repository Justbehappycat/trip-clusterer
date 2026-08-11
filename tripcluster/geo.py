"""Great-circle geometry and GPS backfill helpers."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Spherical mean of (lat, lon) pairs.

    Averaging degrees directly breaks across the antimeridian and near the poles,
    so convert to unit vectors first.
    """
    if not points:
        raise ValueError("centroid of empty point set")
    x = y = z = 0.0
    for lat, lon in points:
        p, l = math.radians(lat), math.radians(lon)
        x += math.cos(p) * math.cos(l)
        y += math.cos(p) * math.sin(l)
        z += math.sin(p)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    hyp = math.hypot(x, y)
    if hyp < 1e-12 and abs(z) < 1e-12:
        # Antipodal cancellation; no meaningful mean. Fall back to first point.
        return points[0]
    return math.degrees(math.atan2(z, hyp)), math.degrees(math.atan2(y, x))


def _lerp_angle(a: float, b: float, t: float) -> float:
    """Interpolate longitude the short way round, so 179 -> -179 spans 2 degrees."""
    d = ((b - a + 180.0) % 360.0) - 180.0
    return ((a + d * t + 180.0) % 360.0) - 180.0


def interpolate_missing_gps(assets: list, max_gap_seconds: int) -> int:
    """Fill lat/lon for assets that have a timestamp but no coordinates.

    Linear interpolation between the nearest time-neighbours that do have
    coordinates. Skipped when either neighbour is further away in time than
    ``max_gap_seconds``. Filled assets are flagged ``inferred`` so they can be
    excluded from stop-centroid calculation.

    ``assets`` must already be sorted by ``ts_utc``. Returns the number filled.
    """
    known = [i for i, a in enumerate(assets) if a.lat is not None and a.lon is not None]
    if len(known) < 2:
        return 0

    filled = 0
    for i, a in enumerate(assets):
        if a.lat is not None and a.lon is not None:
            continue

        # Nearest known neighbour on each side.
        lo = _last_before(known, i)
        hi = _first_after(known, i)
        if lo is None or hi is None:
            continue

        before, after = assets[lo], assets[hi]
        if a.ts_utc - before.ts_utc > max_gap_seconds:
            continue
        if after.ts_utc - a.ts_utc > max_gap_seconds:
            continue

        span = after.ts_utc - before.ts_utc
        t = 0.0 if span <= 0 else (a.ts_utc - before.ts_utc) / span
        a.lat = before.lat + (after.lat - before.lat) * t
        a.lon = _lerp_angle(before.lon, after.lon, t)
        a.inferred = True
        filled += 1

    return filled


def _last_before(sorted_idx: list[int], i: int) -> int | None:
    import bisect

    pos = bisect.bisect_left(sorted_idx, i)
    return sorted_idx[pos - 1] if pos > 0 else None


def _first_after(sorted_idx: list[int], i: int) -> int | None:
    import bisect

    pos = bisect.bisect_right(sorted_idx, i)
    return sorted_idx[pos] if pos < len(sorted_idx) else None


def bounding_points(assets: Iterable) -> list[tuple[float, float]]:
    return [(a.lat, a.lon) for a in assets if a.lat is not None and a.lon is not None]
