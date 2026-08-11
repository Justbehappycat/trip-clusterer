"""Configuration loading. Secrets come from the environment, never the file."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass
class Thresholds:
    # Trip segmentation
    home_radius_km: float = 40.0
    max_gap_hours: float = 36.0
    min_duration_days: float = 2.0
    min_photos: int = 10

    # Stop clustering
    stop_radius_km: float = 50.0
    # 30, not the plan's 12: at 12 an overnight with no photos opens a new stop,
    # so a four-day stay in one city reads as four stops.
    stop_gap_hours: float = 30.0

    # Leg classification
    # 150, not 300: implied speed is measured stop-end to stop-start, so it is
    # always an underestimate of true travel speed. At 300 nothing short of a
    # photo taken on the runway reads as a flight. 150 catches the moderate-gap
    # case; it cannot catch a long gap (see the caveat in README.md), and no
    # value can, because a wide enough gap drags a flight into the driving band.
    flight_min_speed_kmh: float = 150.0
    drive_min_speed_kmh: float = 40.0
    drive_max_speed_kmh: float = 150.0
    # Road-feasibility test. Legs are measured great-circle, but a car drives
    # roads: US interstate routings run about 1.2-1.3x the straight line. If
    # covering that road distance in the elapsed time would demand a sustained
    # average above max_sustained_road_kmh, no car did it.
    #
    # This binds well before flight_min_speed_kmh does (130/1.25 = 104 km/h
    # great-circle), so it is the test that actually fires. The margin against
    # a genuinely fast drive is thin — see the boundary tests in
    # tests/test_cluster.py before changing either number.
    road_circuity_factor: float = 1.25
    max_sustained_road_kmh: float = 130.0
    long_haul_min_km: float = 100.0
    road_trip_min_ground_fraction: float = 1.0

    # GPS backfill
    max_interpolation_gap_hours: float = 6.0


@dataclass
class ApiConfig:
    base_url: str = "http://localhost:2283"
    key: str = ""
    timeout_seconds: float = 60.0
    page_size: int = 1000
    # Endpoint paths are configurable because Immich's REST API moves between
    # releases. Verify these against the live spec with `--check-api` before
    # trusting them; do not assume the defaults are current.
    search_assets: str = "/api/search/metadata"
    create_album: str = "/api/albums"
    update_album: str = "/api/albums/{id}"
    add_album_assets: str = "/api/albums/{id}/assets"
    openapi_spec: str = "/api/specs-json"


@dataclass
class Config:
    home_lat: float
    home_lon: float
    thresholds: Thresholds
    api: ApiConfig
    state_db: Path
    geocoder: str = "offline"        # "offline" (reverse_geocoder) | "none"
    album_prefix: str = ""

    @property
    def home(self) -> tuple[float, float]:
        return (self.home_lat, self.home_lon)


def _subset(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys but naming them."""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys in config: {sorted(unknown)}")
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser()
    raw = yaml.safe_load(path.read_text()) or {}

    home = raw.get("home") or {}
    if "lat" not in home or "lon" not in home:
        raise ValueError(f"{path}: home.lat and home.lon are required")

    api = _subset(ApiConfig, raw.get("api", {}))
    # The API key lives in the environment so config.yaml stays committable.
    api.key = os.environ.get("IMMICH_API_KEY", api.key)
    api.base_url = os.environ.get("IMMICH_BASE_URL", api.base_url).rstrip("/")

    state_db = Path(raw.get("state_db", "trips.db")).expanduser()
    if not state_db.is_absolute():
        state_db = path.parent / state_db

    return Config(
        home_lat=float(home["lat"]),
        home_lon=float(home["lon"]),
        thresholds=_subset(Thresholds, raw.get("thresholds", {})),
        api=api,
        state_db=state_db,
        geocoder=raw.get("geocoder", "offline"),
        album_prefix=raw.get("album_prefix", ""),
    )
