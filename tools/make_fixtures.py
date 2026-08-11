#!/usr/bin/env python3
"""Generate a synthetic photo library so thresholds can be tuned before Immich
exists.

    ./tools/make_fixtures.py > fixtures/synthetic.json
    ./cluster_trips.py --config config.yaml --from-json fixtures/synthetic.json

Home is Los Angeles. The library contains, deliberately:
  - background photos at home across the year          (never a trip)
  - a 9-day LA -> New York drive                       (road trip)
  - Seattle then Denver, with a flight between them     (trip, not road trip)
  - a 1-day Joshua Tree run                            (below min_duration)
  - a stretch of the drive with GPS stripped           (exercises interpolation)
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone

HOME = (34.0522, -118.2437)   # Los Angeles

ROAD_TRIP = [
    ("Los Angeles", 34.0522, -118.2437),
    ("Las Vegas", 36.1699, -115.1398),
    ("Zion", 37.2982, -113.0263),
    ("Moab", 38.5733, -109.5498),
    ("Denver", 39.7392, -104.9903),
    ("Omaha", 41.2565, -95.9345),
    ("Chicago", 41.8781, -87.6298),
    ("Cleveland", 41.4993, -81.6944),
    ("New York", 40.7128, -74.0060),
]

SEATTLE = (47.6062, -122.3321)
DENVER = (39.7392, -104.9903)
JOSHUA_TREE = (33.8734, -115.9010)

_counter = 0


def asset(dt: datetime, lat: float | None, lon: float | None,
          offset_min: int = -420) -> dict:
    global _counter
    _counter += 1
    return {
        "id": f"asset-{_counter:05d}",
        "ts_utc": int(dt.replace(tzinfo=timezone.utc).timestamp()),
        "tz_offset_min": offset_min,
        "lat": lat,
        "lon": lon,
    }


def jitter(lat: float, lon: float, km: float, rng: random.Random) -> tuple[float, float]:
    """Scatter points around a centre — real photos aren't stacked on a pixel."""
    return (lat + rng.uniform(-km, km) / 111.0,
            lon + rng.uniform(-km, km) / 88.0)


def build() -> list[dict]:
    rng = random.Random(1848)
    out: list[dict] = []

    # Background: a few photos a week at home, all year.
    day = datetime(2023, 1, 1, 12)
    while day < datetime(2023, 12, 31):
        for _ in range(rng.randint(0, 3)):
            lat, lon = jitter(*HOME, 8, rng)
            out.append(asset(day + timedelta(hours=rng.randint(0, 10)), lat, lon))
        day += timedelta(days=rng.randint(2, 6))

    # Road trip: one stop per day, ~25 photos each, driving between.
    t = datetime(2023, 9, 2, 9)
    for i, (_, lat, lon) in enumerate(ROAD_TRIP):
        for _ in range(rng.randint(20, 30)):
            plat, plon = jitter(lat, lon, 12, rng)
            when = t + timedelta(hours=rng.uniform(0, 8))
            # Days 4-5 in the Rockies: GPS stripped, timestamps intact.
            if i in (3, 4) and rng.random() < 0.35:
                out.append(asset(when, None, None, -360))
            else:
                out.append(asset(when, plat, plon, -360))
        t += timedelta(days=1)

    # Seattle by air: LA -> SEA in 3 hours implies ~500 km/h.
    t = datetime(2024, 3, 14, 8)
    for _ in range(14):
        lat, lon = jitter(*HOME, 5, rng)
        out.append(asset(t + timedelta(hours=rng.uniform(0, 2)), lat, lon))
    t += timedelta(hours=5)
    for day_i in range(4):
        for _ in range(rng.randint(12, 20)):
            lat, lon = jitter(*SEATTLE, 15, rng)
            out.append(asset(t + timedelta(days=day_i, hours=rng.uniform(0, 9)),
                             lat, lon))
    # ...then Seattle -> Denver by air: ~1650 km in ~5 h, well over the flight
    # threshold, so this trip must not be classified as a road trip.
    t += timedelta(days=4, hours=6)
    for day_i in range(3):
        for _ in range(rng.randint(12, 18)):
            lat, lon = jitter(*DENVER, 12, rng)
            out.append(asset(t + timedelta(days=day_i, hours=rng.uniform(0, 9)),
                             lat, lon, -360))

    # Joshua Tree: one long day. Should fall below min_duration_days.
    t = datetime(2024, 5, 18, 8)
    for _ in range(30):
        lat, lon = jitter(*JOSHUA_TREE, 10, rng)
        out.append(asset(t + timedelta(hours=rng.uniform(0, 11)), lat, lon))

    out.sort(key=lambda a: a["ts_utc"])
    return out


if __name__ == "__main__":
    json.dump(build(), sys.stdout, indent=1)
    sys.stdout.write("\n")
