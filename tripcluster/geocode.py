"""Reverse geocoding of stop centroids only — never per-photo.

Uses the offline ``reverse_geocoder`` package: no rate limits, no network, and
city-level accuracy is all the naming needs. If the package isn't installed the
pipeline still runs; stops just fall back to their coordinates.
"""

from __future__ import annotations

import logging

from .model import Stop

log = logging.getLogger(__name__)


def label_stops(stops: list[Stop], mode: str = "offline") -> None:
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

    for stop, res in zip(stops, results):
        city = (res.get("name") or "").strip()
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
