"""Trip segmentation, stop clustering and leg classification.

Pure functions over :class:`Asset` lists — no network, no database. Everything
here is driven by the thresholds in ``config.yaml``; the defaults are starting
points, not answers.
"""

from __future__ import annotations

from .config import Thresholds
from .geo import centroid, haversine_km
from .model import Asset, Leg, Stop, Trip


def segment_trips(assets: list[Asset], home: tuple[float, float], th: Thresholds) -> list[Trip]:
    """Split a time-sorted asset list into candidate trips.

    A trip is a contiguous run of "away from home" photos with no internal gap
    longer than ``max_gap_hours``. Photos taken at home neither join nor split a
    run — in practice a pass through home shows up as a time gap anyway.
    """
    away = [
        a
        for a in assets
        if a.has_fix and haversine_km(a.lat, a.lon, home[0], home[1]) > th.home_radius_km
    ]
    if not away:
        return []

    max_gap = th.max_gap_hours * 3600
    runs: list[list[Asset]] = [[away[0]]]
    for prev, cur in zip(away, away[1:]):
        if cur.ts_utc - prev.ts_utc > max_gap:
            runs.append([cur])
        else:
            runs[-1].append(cur)

    return [
        Trip(assets=run)
        for run in runs
        if len(run) >= th.min_photos
        and (run[-1].ts_utc - run[0].ts_utc) >= th.min_duration_days * 86400
    ]


def cluster_stops(trip: Trip, th: Thresholds) -> list[Stop]:
    """Group a trip's photos into stops.

    A photo joins the current stop if it is within ``stop_radius_km`` of that
    stop's running centroid *and* within ``stop_gap_hours`` of its latest photo.
    Otherwise it opens a new stop.

    Interpolated coordinates are carried along but excluded from the centroid,
    so a guessed position can't drag a stop's location off the real one.
    """
    stops: list[Stop] = []
    cur_ids: list[str] = []
    cur_points: list[tuple[float, float]] = []   # measured fixes only
    cur_centroid: tuple[float, float] | None = None
    cur_start = cur_end = 0

    def flush() -> None:
        if not cur_ids:
            return
        lat, lon = cur_centroid if cur_centroid else (0.0, 0.0)
        stops.append(
            Stop(lat=lat, lon=lon, start_utc=cur_start, end_utc=cur_end, asset_ids=list(cur_ids))
        )

    for a in trip.assets:
        if not a.has_fix:
            continue

        ref = cur_centroid if cur_centroid is not None else (a.lat, a.lon)
        near = haversine_km(a.lat, a.lon, ref[0], ref[1]) <= th.stop_radius_km
        recent = (a.ts_utc - cur_end) <= th.stop_gap_hours * 3600

        if cur_ids and near and recent:
            cur_ids.append(a.id)
            cur_end = a.ts_utc
            if not a.inferred:
                cur_points.append((a.lat, a.lon))
                cur_centroid = centroid(cur_points)
            continue

        flush()
        cur_ids = [a.id]
        cur_points = [] if a.inferred else [(a.lat, a.lon)]
        # A stop opened by an inferred-only photo still needs somewhere to sit.
        cur_centroid = centroid(cur_points) if cur_points else (a.lat, a.lon)
        cur_start = cur_end = a.ts_utc

    flush()
    return stops


def classify_legs(stops: list[Stop], th: Thresholds) -> list[Leg]:
    """Classify travel between each consecutive stop pair.

    Two independent flight tests, either of which is sufficient:

    1. Implied speed above ``flight_min_speed_kmh``.
    2. Road infeasibility — covering the road distance in the elapsed time
       would require an impossible sustained average.

    The second is the one that fires in practice. It is also the sounder test,
    because elapsed time runs from one stop's last photo to the next stop's
    first photo and so is always *longer* than the real travel time. Implied
    speed is therefore an underestimate, and if even the underestimate demands
    an impossible road pace, the true pace was higher still.
    """
    legs: list[Leg] = []
    for cur, nxt in zip(stops, stops[1:]):
        dist = haversine_km(cur.lat, cur.lon, nxt.lat, nxt.lon)
        # Time between leaving one stop and arriving at the next. Dwell time is
        # absorbed by the stops themselves, so this is mostly travel.
        elapsed = max(nxt.start_utc - cur.end_utc, 0) / 3600.0
        speed = dist / elapsed if elapsed > 0 else float("inf")

        # What a car would have had to average on real roads to do this.
        required_road_kmh = (
            dist * th.road_circuity_factor / elapsed if elapsed > 0 else float("inf")
        )
        road_impossible = (
            dist >= th.long_haul_min_km
            and required_road_kmh > th.max_sustained_road_kmh
        )

        if speed > th.flight_min_speed_kmh or road_impossible:
            kind = "flight"
        elif dist >= th.long_haul_min_km and th.drive_min_speed_kmh <= speed <= th.drive_max_speed_kmh:
            kind = "driving"
        elif dist >= th.long_haul_min_km:
            kind = "ground"      # covered real ground, but not at a driving pace
        else:
            kind = "local"

        legs.append(Leg(distance_km=dist, elapsed_hours=elapsed, kind=kind))
    return legs


def classify_trip(legs: list[Leg], th: Thresholds) -> str:
    """Decide whether a trip counts as a road trip.

    Measured by distance, not leg count: a trip is a road trip when at least
    ``road_trip_min_ground_fraction`` of its long-haul kilometres were covered
    on the ground. At the default 1.0 this is the plan's "all long-haul legs are
    ground-speed" rule. Lower it to ~0.5 to also catch fly-out-then-drive trips.
    """
    long_haul = [leg for leg in legs if leg.distance_km >= th.long_haul_min_km]
    if not long_haul:
        return "trip"

    total = sum(leg.distance_km for leg in long_haul)
    ground = sum(leg.distance_km for leg in long_haul if leg.is_ground)
    if total <= 0:
        return "trip"
    return "road_trip" if (ground / total) >= th.road_trip_min_ground_fraction else "trip"


def build_trips(assets: list[Asset], home: tuple[float, float], th: Thresholds) -> list[Trip]:
    """Full pipeline: segment, cluster stops, classify legs and trip kind."""
    trips = segment_trips(assets, home, th)
    for trip in trips:
        trip.stops = cluster_stops(trip, th)
        trip.legs = classify_legs(trip.stops, th)
        trip.kind = classify_trip(trip.legs, th)
    return trips


def farthest_stop(stops: list[Stop]) -> Stop | None:
    """Stop furthest from the first one — the turnaround point of a road trip."""
    if len(stops) < 2:
        return stops[0] if stops else None
    origin = stops[0]
    return max(stops[1:], key=lambda s: haversine_km(origin.lat, origin.lon, s.lat, s.lon))
