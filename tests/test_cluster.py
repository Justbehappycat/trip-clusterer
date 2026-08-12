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
    build_trips, classify_legs, classify_trip, cluster_stops, max_ground_km,
    segment_trips,
)
from tripcluster.config import Thresholds  # noqa: E402
from tripcluster.geo import (  # noqa: E402
    centroid, drop_impossible_fixes, haversine_km, interpolate_missing_gps,
)
from tripcluster.model import Asset, Home, Stop, Trip  # noqa: E402
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


# ---- impossible coordinates -----------------------------------------------

def test_one_bad_fix_between_two_good_ones_is_discarded():
    """The real case: a single photo in Cheshire inside an Arizona road trip.

    It was a genuine iPhone HEIC — not a saved internet image — so provenance
    could not have caught it. What gives it away is that it is impossible in
    *both* directions: real travel is fast one way, because you arrive and
    stay.
    """
    assets = track(DENVER, ts(2023, 6, 1, 8), 5, spacing_h=1)
    assets.append(Asset(id="bad", ts_utc=ts(2023, 6, 1, 10, ),
                        lat=53.3781, lon=-2.5620))          # Stockton Heath, GB
    assets += track(DENVER, ts(2023, 6, 1, 13), 5, spacing_h=1, prefix="b")
    assets.sort(key=lambda a: a.ts_utc)

    assert drop_impossible_fixes(assets, 1000.0) == 1
    bad = next(a for a in assets if a.id == "bad")
    assert not bad.has_fix                    # coordinates cleared
    assert bad in assets                      # but the photo is still yours
    assert all(a.has_fix for a in assets if a.id != "bad")


def test_a_real_flight_is_not_mistaken_for_a_bad_fix():
    """Arriving somewhere fast and staying is travel, not an error."""
    assets = (track(SEATTLE, ts(2023, 6, 1, 6), 5, spacing_h=1)
              + track(NEW_YORK, ts(2023, 6, 1, 14), 5, spacing_h=1, prefix="b"))
    assets.sort(key=lambda a: a.ts_utc)
    assert drop_impossible_fixes(assets, 1000.0) == 0


def test_in_flight_window_photos_survive():
    """Photos shot out of the window are real positions, not bad fixes.

    These appear in the library as Kamchatka and North Korea stops on
    trans-Pacific legs. Cruise is ~900-1000 km/h, so the threshold sits above
    it deliberately: the legs here are 966 km and 1066 km in an hour each.
    """
    assets = [
        Asset(id="a", ts_utc=ts(2023, 6, 1, 6), lat=47.6, lon=-122.3),
        Asset(id="w1", ts_utc=ts(2023, 6, 1, 7), lat=50.0, lon=-135.0),
        Asset(id="w2", ts_utc=ts(2023, 6, 1, 8), lat=51.5, lon=-150.0),
        Asset(id="b", ts_utc=ts(2023, 6, 1, 14), lat=35.7, lon=139.7),
    ]
    assert drop_impossible_fixes(assets, 1000.0) == 0


# ---- multiple and time-bounded homes --------------------------------------

DALIAN = (38.9140, 121.6147)


def test_a_second_home_is_not_a_trip():
    """Two homes at once: a stay at either is ordinary life, not travel.

    With a single home point, every stay at the other one clusters and gets
    named as a trip — the symptom that made months of Dalian albums appear
    alongside real travel.
    """
    assets = track(DALIAN, ts(2025, 1, 24), 40, spacing_h=4)
    one_home = segment_trips(assets, HOME, TH)
    assert one_home, "sanity: with only LA as home this looks like a trip"

    both = [Home(lat=HOME[0], lon=HOME[1]), Home(lat=DALIAN[0], lon=DALIAN[1])]
    assert segment_trips(assets, both, TH) == []


def test_moving_house_does_not_make_the_old_address_travel():
    """Homes are time-bounded, so each era is judged against its own home."""
    orange = (33.7879, -117.8531)
    moved = ts(2025, 1, 1)
    homes = [
        Home(lat=orange[0], lon=orange[1], label="Orange", until_utc=moved),
        Home(lat=HOME[0], lon=HOME[1], label="LA", from_utc=moved),
    ]
    # Living in Orange before the move, and in LA after it.
    before = track(orange, ts(2024, 6, 1), 30, spacing_h=6)
    after = track(HOME, ts(2025, 6, 1), 30, spacing_h=6, prefix="b")
    assert segment_trips(before + after, homes, TH) == []

    # The same two runs against a single fixed home: one era reads as travel.
    assert segment_trips(before + after, HOME, TH)


def test_a_home_only_suppresses_travel_while_it_is_in_effect():
    """Before you lived there, the new address is a place you visited."""
    moved = ts(2025, 1, 1)
    homes = [Home(lat=DALIAN[0], lon=DALIAN[1], from_utc=moved),
             Home(lat=HOME[0], lon=HOME[1])]
    visit = track(DALIAN, ts(2024, 3, 2), 30, spacing_h=4)   # a year too early
    assert segment_trips(visit, homes, TH), "a pre-move stay is still a trip"

    living = track(DALIAN, ts(2025, 3, 2), 30, spacing_h=4)
    assert segment_trips(living, homes, TH) == []


def test_bare_coordinates_still_work():
    """The original (lat, lon) form has to keep working — it is the common case."""
    away = track(VEGAS, ts(2023, 6, 1), 30, spacing_h=4)
    assert segment_trips(away, HOME, TH)
    assert segment_trips(away, Home(lat=HOME[0], lon=HOME[1]), TH)
    assert segment_trips(away, [Home(lat=HOME[0], lon=HOME[1])], TH)


def test_legs_are_measured_between_boundary_photos_not_centroids():
    """A wide stop must not manufacture distance the traveller never covered.

    Both stops here sprawl ~90 km east-west, but the photo leaving the first
    and the photo arriving at the second are close together — the drive
    between them is short. Centroid-to-centroid measurement invented ~90 km of
    travel in the ten minutes between those photos, which then read as
    impossible for a car and turned interstate legs inside real road trips
    into flights.
    """
    base = ts(2023, 6, 1, 8)
    # Stop A sits around -112.0 but its last photo is 38 km east of its own
    # centroid; stop B's first photo is 15 km further on, and B's mass then
    # pulls its centroid a long way east. The two centroids end up ~89 km
    # apart while the photos bracketing the drive are ~15 km apart.
    a = [Asset(id=f"a{i}", ts_utc=base + i * 1800, lat=40.0, lon=-112.0)
         for i in range(6)]
    a.append(Asset(id="a-last", ts_utc=base + 6 * 1800, lat=40.0, lon=-111.47))
    b = [Asset(id="b-first", ts_utc=base + 6 * 1800 + 600, lat=40.0, lon=-111.29)]
    b += [Asset(id=f"b{i}", ts_utc=base + 7 * 1800 + i * 1800, lat=40.0, lon=-110.8)
          for i in range(6)]
    trip = Trip(assets=a + b)
    trip.stops = cluster_stops(trip, TH)
    assert len(trip.stops) == 2

    leg = classify_legs(trip.stops, TH)[0]
    centroid_km = haversine_km(trip.stops[0].lat, trip.stops[0].lon,
                               trip.stops[1].lat, trip.stops[1].lon)
    assert leg.distance_km < centroid_km, "centroids overstate the travel"
    assert leg.kind != "flight"


def test_boundary_points_fall_back_to_the_centroid():
    """A Stop built without boundary points still measures — find_home does this."""
    s = Stop(lat=10.0, lon=20.0, start_utc=0, end_utc=1)
    assert s.start_point == (10.0, 20.0)
    assert s.end_point == (10.0, 20.0)


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


def test_moderate_photo_gap_flight_is_caught_at_the_default():
    """Seattle -> Denver with a 7-hour photo gap: ~234 km/h implied.

    Implied speed is measured stop-end to stop-start, so it absorbs airport
    time and understates the real pace. At the old 300 km/h default this leg
    read as ground travel and the trip as a road trip. Nothing on the ground
    sustains 234 km/h over 1600 km, and the 150 default now catches it.
    """
    assets = track(SEATTLE, ts(2023, 6, 1, 6), 6, spacing_h=1) + track(
        DENVER, ts(2023, 6, 1, 18), 6, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)

    assert 200 < legs[0].implied_speed_kmh < 260
    assert legs[0].kind == "flight"
    assert classify_trip(legs, TH) == "trip"

    # How this looked before either fix: the old 300 km/h default *and* no
    # road-feasibility test. Kept to document what the bug actually was.
    # Note the feasibility test alone now catches this even at 300, so
    # restoring the old threshold is no longer sufficient to reintroduce it.
    old = Thresholds(flight_min_speed_kmh=300.0, max_sustained_road_kmh=10_000.0)
    assert classify_legs(trip.stops, old)[0].kind == "ground"
    assert classify_trip(classify_legs(trip.stops, old), old) == "road_trip"


def test_wide_photo_gap_flight_is_caught_by_road_infeasibility():
    """The case no *speed threshold* fixes, now caught by a different test.

    Same Seattle -> Denver flight, but nobody shoots for 15 hours around it.
    That implies ~109 km/h — an ordinary driving pace — so any flight_min_speed
    low enough to catch it would also reclassify genuine road trips.

    Road feasibility catches it without that trade. 1640 km great-circle is
    ~2050 km of road; covering it in 15 hours needs a sustained 137 km/h, which
    no car does. The test is sound in the right direction: elapsed time runs
    stop-end to stop-start and so overstates travel time, meaning the required
    pace computed here is a *lower bound* on what really happened.
    """
    assets = track(SEATTLE, ts(2023, 6, 1, 6), 6, spacing_h=1) + track(
        DENVER, ts(2023, 6, 2, 2), 6, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)

    assert 90 < legs[0].implied_speed_kmh < 130      # looks like driving
    assert legs[0].kind == "flight"                  # but no car could do it
    assert classify_trip(legs, TH) == "trip"

    # Speed alone still cannot see it — this is what the new test buys.
    speed_only = Thresholds(max_sustained_road_kmh=10_000.0)
    assert classify_legs(trip.stops, speed_only)[0].kind == "driving"


def test_the_same_speed_is_a_drive_over_hours_and_a_flight_over_a_day():
    """Sustainability depends on duration — one speed ceiling cannot say this.

    ~110 km/h great-circle is a brisk interstate run for two hours and an
    impossibility for fifteen, because nobody drives fifteen hours without
    stopping. A fixed ceiling has to either reject the first or accept the
    second; the duty-cycle model gets both right.
    """
    short_km = 110.0 * 2.0
    long_km = 110.0 * 15.0
    assert max_ground_km(2.0, TH) > short_km, "a 2-hour run at this pace is drivable"
    assert max_ground_km(15.0, TH) < long_km, "15 hours at this pace is not"

    # And the ceiling never decreases — more time is never less reach.
    reach = [max_ground_km(h, TH) for h in (1, 2, 4, 8, 12, 24, 48)]
    assert reach == sorted(reach)


def test_a_hard_but_real_drive_is_not_called_a_flight():
    """The false-positive guard: 900 km in 9 h is a long day, not a flight.

    100 km/h great-circle needs ~125 km/h on road, under the 130 ceiling. This
    is the closest realistic drive to the cutoff, so it is the test that breaks
    first if max_sustained_road_kmh is lowered or circuity is raised.
    """
    omaha = (41.2565, -95.9345)
    assets = track(DENVER, ts(2023, 6, 1, 6), 4, spacing_h=1) + track(
        omaha, ts(2023, 6, 1, 18), 4, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)
    assert legs[0].kind == "driving"
    assert classify_trip(legs, TH) == "road_trip"


def test_short_flight_with_a_long_gate_gap_is_still_a_known_miss():
    """The limitation that survives — pinned so it stays visible.

    Seattle -> Spokane is ~360 km. With 5 hours between the last photo in one
    city and the first in the other, that implies ~72 km/h, which is simply a
    drive. Road feasibility does not help: ~90 km/h on road is entirely
    possible. Nothing in the photo track distinguishes this from driving the
    same route, so it is recorded rather than chased.
    """
    spokane = (47.6588, -117.4260)
    assets = track(SEATTLE, ts(2023, 6, 1, 6), 4, spacing_h=1) + track(
        spokane, ts(2023, 6, 1, 13), 4, spacing_h=1, prefix="b"
    )
    trip = Trip(assets=assets)
    trip.stops = cluster_stops(trip, TH)
    legs = classify_legs(trip.stops, TH)

    assert legs[0].implied_speed_kmh < 104           # under the cutoff
    assert legs[0].kind in ("driving", "ground")     # wrong, and knowingly so


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
    """Vegas -> Denver -> Omaha -> New York, at a pace a car can actually keep.

    The Omaha stop is not decoration. Denver to New York direct is 2,620 km,
    and the road-feasibility test rejects that in the two days this trip would
    otherwise allow — correctly, since it would mean driving 26 hours straight.
    A real cross-country drive breaks somewhere in Nebraska.
    """
    omaha = (41.2565, -95.9345)
    assets = (
        track(VEGAS, ts(2023, 9, 2, 8), 12, spacing_h=2)
        + track(DENVER, ts(2023, 9, 4, 8), 12, spacing_h=2, prefix="b")
        + track(omaha, ts(2023, 9, 6, 8), 12, spacing_h=2, prefix="c")
        + track(NEW_YORK, ts(2023, 9, 8, 8), 12, spacing_h=2, prefix="d")
    )
    trips = build_trips(assets, HOME, TH)
    assert len(trips) == 1
    trip = trips[0]
    assert trip.kind == "road_trip"
    for stop, name in zip(trip.stops, ["Las Vegas", "Denver", "Omaha", "New York"]):
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
