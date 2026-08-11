"""Pipeline tests over synthetic tracks — no Immich instance required.

    python3 -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripcluster.cluster import (  # noqa: E402
    build_trips, classify_legs, classify_trip, cluster_stops, segment_trips,
)
from tripcluster.config import Thresholds  # noqa: E402
from tripcluster.geo import centroid, haversine_km, interpolate_missing_gps  # noqa: E402
from tripcluster.model import Asset, Trip  # noqa: E402
from tripcluster.naming import describe, heuristic_name  # noqa: E402
from tripcluster.store import Store  # noqa: E402

HOME = (34.0522, -118.2437)     # Los Angeles
VEGAS = (36.1699, -115.1398)
DENVER = (39.7392, -104.9903)
NEW_YORK = (40.7128, -74.0060)
SEATTLE = (47.6062, -122.3321)

TH = Thresholds()


def ts(y, m, d, h=12) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def track(place, start_ts, n, spacing_h=1, prefix="a", gps=True) -> list[Asset]:
    lat, lon = place
    return [
        Asset(
            id=f"{prefix}{i}",
            ts_utc=start_ts + i * spacing_h * 3600,
            tz_offset_min=-420,
            lat=lat + i * 0.001 if gps else None,
            lon=lon + i * 0.001 if gps else None,
        )
        for i in range(n)
    ]


# ---- geometry -------------------------------------------------------------

def test_haversine_known_distance():
    # LA -> NY is ~3936 km.
    d = haversine_km(*HOME, *NEW_YORK)
    assert 3900 < d < 3980


def test_centroid_crosses_antimeridian():
    lat, lon = centroid([(0.0, 179.0), (0.0, -179.0)])
    assert abs(lat) < 1e-6
    assert abs(abs(lon) - 180.0) < 1e-6, f"got {lon}, expected ~±180"


# ---- GPS backfill ---------------------------------------------------------

def test_interpolation_fills_between_near_neighbours():
    assets = [
        Asset(id="a", ts_utc=0, lat=10.0, lon=20.0),
        Asset(id="b", ts_utc=3600),
        Asset(id="c", ts_utc=7200, lat=12.0, lon=24.0),
    ]
    assert interpolate_missing_gps(assets, 6 * 3600) == 1
    assert assets[1].lat == pytest.approx(11.0)
    assert assets[1].lon == pytest.approx(22.0)
    assert assets[1].inferred is True


def test_interpolation_skips_distant_neighbours():
    assets = [
        Asset(id="a", ts_utc=0, lat=10.0, lon=20.0),
        Asset(id="b", ts_utc=20 * 3600),
        Asset(id="c", ts_utc=40 * 3600, lat=12.0, lon=24.0),
    ]
    assert interpolate_missing_gps(assets, 6 * 3600) == 0
    assert assets[1].lat is None


def test_interpolation_takes_short_way_round_antimeridian():
    assets = [
        Asset(id="a", ts_utc=0, lat=0.0, lon=179.0),
        Asset(id="b", ts_utc=3600),
        Asset(id="c", ts_utc=7200, lat=0.0, lon=-179.0),
    ]
    interpolate_missing_gps(assets, 6 * 3600)
    assert abs(abs(assets[1].lon) - 180.0) < 1e-6


# ---- segmentation ---------------------------------------------------------

def test_home_photos_are_never_a_trip():
    assets = track(HOME, ts(2023, 6, 1), 100, spacing_h=6)
    assert segment_trips(assets, HOME, TH) == []


def test_short_trip_is_discarded():
    # One long day away: enough photos, not enough days.
    assets = track(VEGAS, ts(2023, 6, 1, 8), 30, spacing_h=0.3)
    assert segment_trips(assets, HOME, TH) == []


def test_sparse_trip_is_discarded():
    # Long enough, but only 5 photos.
    assets = track(VEGAS, ts(2023, 6, 1), 5, spacing_h=24)
    assert segment_trips(assets, HOME, TH) == []


def test_long_gap_splits_one_run_into_two_trips():
    first = track(VEGAS, ts(2023, 6, 1), 20, spacing_h=6, prefix="x")
    second = track(VEGAS, ts(2023, 8, 1), 20, spacing_h=6, prefix="y")
    trips = segment_trips(first + second, HOME, TH)
    assert len(trips) == 2


def test_qualifying_trip_is_kept():
    assets = track(VEGAS, ts(2023, 6, 1), 40, spacing_h=3)
    trips = segment_trips(assets, HOME, TH)
    assert len(trips) == 1
    assert trips[0].photo_count == 40
    assert trips[0].duration_days > 2


# ---- stop clustering ------------------------------------------------------

def test_distant_photos_open_a_new_stop():
    assets = track(VEGAS, ts(2023, 6, 1), 10, spacing_h=2) + track(
        DENVER, ts(2023, 6, 2), 10, spacing_h=2, prefix="b"
    )
    stops = cluster_stops(Trip(assets=assets), TH)
    assert len(stops) == 2
    assert haversine_km(stops[0].lat, stops[0].lon, *VEGAS) < 5
    assert haversine_km(stops[1].lat, stops[1].lon, *DENVER) < 5


def test_time_gap_alone_opens_a_new_stop():
    # Same place, but two visits a week apart.
    assets = track(VEGAS, ts(2023, 6, 1), 10, spacing_h=1) + track(
        VEGAS, ts(2023, 6, 8), 10, spacing_h=1, prefix="b"
    )
    stops = cluster_stops(Trip(assets=assets), TH)
    assert len(stops) == 2


def test_inferred_coordinates_do_not_move_the_centroid():
    assets = track(VEGAS, ts(2023, 6, 1), 10, spacing_h=1)
    stray = Asset(id="bad", ts_utc=ts(2023, 6, 1) + 5 * 3600,
                  lat=VEGAS[0] + 0.3, lon=VEGAS[1] + 0.3, inferred=True)
    assets.append(stray)
    assets.sort(key=lambda a: a.ts_utc)

    stops = cluster_stops(Trip(assets=assets), TH)
    assert len(stops) == 1
    assert "bad" in stops[0].asset_ids          # still filed under the stop
    assert haversine_km(stops[0].lat, stops[0].lon, *VEGAS) < 3   # but not steering it


# ---- leg + trip classification -------------------------------------------

def test_flight_leg_detected_by_implied_speed():
    # Last Seattle photo 09:00, first Denver photo 13:00: ~1640 km in 4 h.
    assets = track(SEATTLE, ts(2023, 6, 1, 6), 4, spacing_h=1) + track(
        DENVER, ts(2023, 6, 1, 13), 6, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)
    assert len(legs) == 1
    assert legs[0].kind == "flight"
    assert classify_trip(legs, TH) == "trip"


def test_flight_threshold_is_sensitive_to_photo_gaps():
    """A real flight can read as ground travel at the default 300 km/h.

    Implied speed is measured stop-end to stop-start, so it includes airport
    time and any stretch where nobody took a photo. Seattle -> Denver with a
    7-hour photo gap implies only ~234 km/h. Nothing on the ground sustains
    that over 1600 km, which is why flight_min_speed_kmh is tunable and why
    150 is the more defensible setting.
    """
    assets = track(SEATTLE, ts(2023, 6, 1, 6), 6, spacing_h=1) + track(
        DENVER, ts(2023, 6, 1, 18), 6, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)

    assert classify_legs(trip.stops, TH)[0].kind == "ground"
    assert classify_trip(classify_legs(trip.stops, TH), TH) == "road_trip"

    strict = Thresholds(flight_min_speed_kmh=150.0)
    assert classify_legs(trip.stops, strict)[0].kind == "flight"
    assert classify_trip(classify_legs(trip.stops, strict), strict) == "trip"


def test_driving_pace_reads_as_a_road_trip():
    # Denver -> Omaha overnight: ~860 km, well inside the driving band.
    omaha = (41.2565, -95.9345)
    assets = track(DENVER, ts(2023, 6, 1, 8), 6, spacing_h=1) + track(
        omaha, ts(2023, 6, 2, 8), 6, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)
    assert legs[0].kind == "driving"
    assert classify_trip(legs, TH) == "road_trip"


def test_ground_fraction_threshold_admits_fly_out_then_drive():
    """A trip that flies out and drives back is not a road trip at 1.0, but is
    at 0.5 — the knob that exists because the strict rule misses that case."""
    assets = (
        track(HOME, ts(2023, 6, 1, 6), 6, spacing_h=1)          # not used as home here
        + track(DENVER, ts(2023, 6, 1, 14), 6, spacing_h=1, prefix="b")
        + track(NEW_YORK, ts(2023, 6, 4, 10), 6, spacing_h=1, prefix="c")
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)
    kinds = [leg.kind for leg in legs]
    assert "flight" in kinds

    assert classify_trip(legs, TH) == "trip"
    lenient = Thresholds(road_trip_min_ground_fraction=0.4)
    assert classify_trip(legs, lenient) == "road_trip"


def test_local_hops_alone_are_not_a_road_trip():
    assets = track(VEGAS, ts(2023, 6, 1), 10, spacing_h=2) + track(
        (VEGAS[0] + 0.6, VEGAS[1]), ts(2023, 6, 3), 10, spacing_h=2, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    assert classify_trip(classify_legs(trip.stops, TH), TH) == "trip"


# ---- naming ---------------------------------------------------------------

def test_road_trip_named_origin_to_farthest():
    assets = (
        track(VEGAS, ts(2023, 9, 2, 8), 12, spacing_h=2)
        + track(DENVER, ts(2023, 9, 4, 8), 12, spacing_h=2, prefix="b")
        + track(NEW_YORK, ts(2023, 9, 6, 8), 12, spacing_h=2, prefix="c")
    )
    trips = build_trips(assets, HOME, TH)
    assert len(trips) == 1
    trip = trips[0]
    for stop, name in zip(trip.stops, ["Las Vegas", "Denver", "New York"]):
        stop.place = name
    trip.heuristic_name = heuristic_name(trip)
    assert trip.heuristic_name == "Las Vegas → New York · Sep 2023"


def test_non_road_trip_named_for_its_busiest_stop():
    assets = track(SEATTLE, ts(2024, 3, 14), 40, spacing_h=3)
    trips = build_trips(assets, HOME, TH)
    trips[0].stops[0].place = "Seattle"
    assert heuristic_name(trips[0]) == "Seattle · Mar 2024"


def test_month_spanning_trip_shows_both_months():
    assets = track(VEGAS, ts(2023, 12, 28), 40, spacing_h=6)
    trips = build_trips(assets, HOME, TH)
    trips[0].stops[0].place = "Las Vegas"
    assert "Dec 2023-Jan 2024" in heuristic_name(trips[0])


def test_description_lists_stops_in_order():
    assets = (
        track(VEGAS, ts(2023, 9, 2, 8), 12, spacing_h=2)
        + track(DENVER, ts(2023, 9, 4, 8), 12, spacing_h=2, prefix="b")
    )
    trips = build_trips(assets, HOME, TH)
    for stop, name in zip(trips[0].stops, ["Las Vegas", "Denver"]):
        stop.place = name
    text = describe(trips[0])
    assert text.index("Las Vegas") < text.index("Denver")
    assert "2 stops" in text
    assert "24 photos" in text


# ---- state / idempotency --------------------------------------------------

@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "trips.db") as s:
        yield s


def make_trip(prefix="a", n=40) -> Trip:
    trip = build_trips(track(VEGAS, ts(2023, 6, 1), n, spacing_h=3, prefix=prefix),
                       HOME, TH)[0]
    trip.heuristic_name = "Las Vegas · Jun 2023"
    return trip


def test_rerun_matches_the_same_trip_and_reuses_its_album(store):
    first = make_trip()
    first.album_id = "album-123"
    store.save_trip(first, "desc")

    # Same photos, plus a few imported later.
    extra = track(VEGAS, ts(2023, 6, 1) + 40 * 3 * 3600, 6, spacing_h=3, prefix="late")
    second = build_trips(
        track(VEGAS, ts(2023, 6, 1), 40, spacing_h=3) + extra, HOME, TH
    )[0]
    store.hydrate(second)

    assert second.trip_id == first.trip_id
    assert second.album_id == "album-123"
    assert len(store.new_asset_ids(second)) == 6


def test_custom_name_survives_a_rerun(store):
    trip = make_trip()
    store.save_trip(trip, "desc")
    store.set_custom_name(trip.trip_id, "The Big One")

    rerun = make_trip()
    store.hydrate(rerun)
    assert rerun.custom_name == "The Big One"
    assert rerun.display_name == "The Big One"

    # Saving again must not clobber the hand-edited name.
    rerun.heuristic_name = "Las Vegas · Jun 2023 (recomputed)"
    store.save_trip(rerun, "desc2")
    assert store.list_trips()[0]["custom_name"] == "The Big One"


def test_interpolated_positions_are_not_cached(store):
    assets = [
        Asset(id="a", ts_utc=0, lat=10.0, lon=20.0),
        Asset(id="b", ts_utc=3600, lat=11.0, lon=22.0, inferred=True),
    ]
    assert store.upsert_assets(assets) == 1
    assert store.known_asset_ids() == {"a"}


def test_unrelated_trips_do_not_collide(store):
    a = make_trip(prefix="a")
    store.save_trip(a, "desc")
    b = make_trip(prefix="z")
    store.hydrate(b)
    assert b.trip_id is None
