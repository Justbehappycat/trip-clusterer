"""Reverse geocoding of stop centroids only — never per-photo.

Uses the offline ``reverse_geocoder`` package: no rate limits, no network, and
city-level accuracy is all the naming needs. If the package isn't installed the
pipeline still runs; stops just fall back to their coordinates.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from .geo import haversine_km
from .model import Stop

log = logging.getLogger(__name__)


_MAJOR_CITIES: list[tuple[str, str, float, float, int]] | None = None


def _major_cities() -> list[tuple[str, str, float, float, int]]:
    """GeoNames places over 100k people: (name, cc, lat, lon, pop), largest first.

    Loaded once. Missing file is not fatal — naming falls back to whatever the
    nearest small place is called, which is what it did before.
    """
    global _MAJOR_CITIES
    if _MAJOR_CITIES is not None:
        return _MAJOR_CITIES

    path = Path(__file__).resolve().parent.parent / "mapdata" / "major_cities.csv"
    cities: list[tuple[str, str, float, float, int]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cities.append((row["name"], row["cc"],
                               float(row["lat"]), float(row["lon"]),
                               int(row["population"])))
    except (OSError, KeyError, ValueError) as e:
        log.warning("no major-city list at %s (%s); stops keep their nearest "
                    "small-place name", path, e)
    _MAJOR_CITIES = cities
    return cities


def _reach_km(population: int, base_km: float) -> float:
    """How far a city's name plausibly extends from its centre point.

    A single radius cannot serve both ends: 30 km captures Xi'an's districts
    but also renames Beaumont, California to Moreno Valley. Cities sprawl
    roughly with the cube root of population, and the dataset gives only a
    centre point, so the reach scales the same way and is capped.

        100k -> base (10 km)     3M -> ~28 km     9.6M -> ~40 km
    """
    scale = max(population, 100_000) / 100_000
    return min(base_km * scale ** 0.3, 40.0)


def _nearby_major_city(lat: float, lon: float, base_km: float,
                       country: str = "") -> str | None:
    """The nearest city over 100k, if the stop is plausibly inside it.

    GeoNames' cities1000 list — which both reverse_geocoder and Immich derive
    from — names a point by the nearest *populated place*, and in dense
    countries that is a subdistrict nobody recognises. Photos in Lanzhou come
    back as "Xihu", Xi'an as "Yanta" or "Lianhu", Beijing as "Wangjing"; Las
    Vegas comes back as "Winchester" or "Paradise".

    The gate is distance. Collapsing everything to the nearest big city would
    ruin the good names: "Estes Park" and "Kanab" are exactly right, and the
    nearest 100k city is 60+ km away from both.

    Nearest wins, not largest. Preferring the largest renamed Rancho Cucamonga
    — a real city of 175k — to its bigger neighbour Fontana, which is worse
    than the name it replaced.

    ``country`` restricts candidates to the same country as the stop. Without
    it, Fort Bliss in Texas became Ciudad Juárez: a larger city, nearby, and
    across an international border.
    """
    best, best_km = None, None
    for name, ccode, clat, clon, pop in _major_cities():
        if country and ccode != country:
            continue
        d = haversine_km(lat, lon, clat, clon)
        if d > _reach_km(pop, base_km):
            continue
        if best_km is None or d < best_km:
            best, best_km = (name, clat, clon), d
    return best


def label_stops(stops: list[Stop], mode: str = "offline",
                major_city_base_km: float = 10.0) -> None:
    """Set ``stop.place`` in-place for every stop across all trips."""
    if mode == "none" or not stops:
        return
    if mode != "offline":
        raise ValueError(f"unsupported geocoder mode: {mode!r}")

    try:
        import reverse_geocoder
    except ImportError:
        log.warning(
            "reverse_geocoder not installed - stops will be named by coordinates. "
            "Install with: pip install reverse_geocoder"
        )
        return

    coords = [(s.lat, s.lon) for s in stops]
    # mode=1 is single-threaded; the multiprocessing path needs a __main__ guard
    # and buys nothing for the few hundred centroids we ever pass it.
    results = reverse_geocoder.search(coords, mode=1)

    # Where a stop resolves to a nearby major city, take that city's own
    # region too. Mixing the two sources produced "New York City, NJ": the
    # stop is in Hoboken, so the state came from New Jersey while the name
    # came from across the river.
    subs = [
        _nearby_major_city(s.lat, s.lon, major_city_base_km,
                           (r.get("cc") or "").strip())
        for s, r in zip(stops, results)
    ]
    sub_points = [(m[1], m[2]) for m in subs if m]
    sub_results = iter(reverse_geocoder.search(sub_points, mode=1)
                       if sub_points else [])

    for stop, res, major in zip(stops, results, subs):
        if major:
            res = next(sub_results)
        city = (major[0] if major else (res.get("name") or "")).strip()
        admin = (res.get("admin1") or "").strip()
        country = (res.get("cc") or "").strip()

        if city and admin and country == "US":
            stop.place = f"{city}, {_state_abbr(admin)}"
        elif city and country and country != "US":
            stop.place = f"{city}, {country}"
        elif city:
            stop.place = city


_US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def _state_abbr(admin1: str) -> str:
    return _US_STATES.get(admin1, admin1)
