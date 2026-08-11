"""Configuration loading. Secrets come from the environment, never the file."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .model import Home


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
    # Every home, in config order. A single `home:` becomes a one-element list,
    # so downstream code only ever deals with the list form.
    homes: list = None               # list[Home]; defaulted in __post_init__

    def __post_init__(self) -> None:
        if not self.homes:
            self.homes = [Home(lat=self.home_lat, lon=self.home_lon)]

    @property
    def home(self):
        """What segment_trips should measure against.

        Returns the full home list. A bare (lat, lon) still works downstream —
        cluster.as_homes normalises either form — but callers that want the
        primary point should use home_lat/home_lon.
        """
        return self.homes


def _epoch(value, field: str, path) -> int | None:
    """YYYY-MM-DD (or a YAML date) to epoch seconds, UTC midnight."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day,
                            tzinfo=timezone.utc).timestamp())
    try:
        d = datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(
            f"{path}: {field} must be YYYY-MM-DD, got {value!r}"
        ) from None
    return int(d.timestamp())


def _parse_homes(raw: dict, path) -> list[Home]:
    """Read `homes:` (a list) or `home:` (a single mapping). At least one required.

    Both forms are supported because a single fixed home is the common case and
    was the original schema; `homes:` exists for people who moved house or keep
    a second home, where one point makes ordinary life look like travel.
    """
    entries = raw.get("homes")
    if entries is None:
        single = raw.get("home")
        if not single:
            raise ValueError(
                f"{path}: either `home:` (lat/lon) or `homes:` (a list) is required"
            )
        entries = [single]
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: `homes:` must be a non-empty list")

    out: list[Home] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or "lat" not in e or "lon" not in e:
            raise ValueError(f"{path}: homes[{i}] needs lat and lon")
        unknown = set(e) - {"lat", "lon", "label", "from", "until"}
        if unknown:
            raise ValueError(f"{path}: homes[{i}] has unknown keys {sorted(unknown)}")
        h = Home(
            lat=float(e["lat"]),
            lon=float(e["lon"]),
            label=str(e.get("label", "")),
            from_utc=_epoch(e.get("from"), f"homes[{i}].from", path),
            until_utc=_epoch(e.get("until"), f"homes[{i}].until", path),
        )
        if (h.from_utc is not None and h.until_utc is not None
                and h.from_utc >= h.until_utc):
            raise ValueError(f"{path}: homes[{i}] has from >= until")
        out.append(h)
    return out


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

    homes = _parse_homes(raw, path)
    home = {"lat": homes[0].lat, "lon": homes[0].lon}

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
        homes=homes,
    )
