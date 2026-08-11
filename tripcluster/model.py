"""Core data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Asset:
    """One photo, normalised out of whatever shape the Immich API returned."""

    id: str
    ts_utc: int                      # epoch seconds, UTC
    tz_offset_min: int | None = None  # EXIF UTC offset, minutes east of UTC
    lat: float | None = None
    lon: float | None = None
    inferred: bool = False           # coordinates were interpolated, not measured

    @property
    def local_dt(self) -> datetime:
        """Wall-clock time where the photo was taken.

        Falls back to UTC when the EXIF offset is missing, which only affects
        display and month-boundary naming.
        """
        off = timedelta(minutes=self.tz_offset_min or 0)
        return datetime.fromtimestamp(self.ts_utc, timezone.utc).astimezone(
            timezone(off)
        )

    @property
    def has_fix(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Home:
    """One place that counts as "not travelling", optionally time-bounded.

    A single fixed point cannot describe a real life: people move house, and
    plenty of people have two homes at once. Both cases produce the same
    symptom — ordinary life at the unmodelled home is clustered and named as
    a trip.

    ``from_utc``/``until_utc`` are inclusive-exclusive epoch bounds; None means
    unbounded on that side, so a home with neither is always in effect.
    """

    lat: float
    lon: float
    label: str = ""
    from_utc: int | None = None
    until_utc: int | None = None

    def covers(self, ts_utc: int) -> bool:
        if self.from_utc is not None and ts_utc < self.from_utc:
            return False
        if self.until_utc is not None and ts_utc >= self.until_utc:
            return False
        return True


@dataclass
class Stop:
    """A cluster of photos in one place, within one trip."""

    lat: float
    lon: float
    start_utc: int
    end_utc: int
    asset_ids: list[str] = field(default_factory=list)
    place: str | None = None         # filled by reverse geocoding

    @property
    def photo_count(self) -> int:
        return len(self.asset_ids)

    @property
    def label(self) -> str:
        return self.place or f"{self.lat:.2f},{self.lon:.2f}"


@dataclass
class Leg:
    """Travel between two consecutive stops."""

    distance_km: float
    elapsed_hours: float
    kind: str                        # "flight" | "driving" | "ground" | "local"

    @property
    def implied_speed_kmh(self) -> float:
        return self.distance_km / self.elapsed_hours if self.elapsed_hours > 0 else 0.0

    @property
    def is_ground(self) -> bool:
        return self.kind != "flight"


@dataclass
class Trip:
    assets: list[Asset]
    stops: list[Stop] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    kind: str = "trip"               # "road_trip" | "trip"
    heuristic_name: str = ""

    # Populated on reconciliation against the state DB.
    trip_id: int | None = None
    album_id: str | None = None
    custom_name: str | None = None

    @property
    def start_utc(self) -> int:
        return self.assets[0].ts_utc

    @property
    def end_utc(self) -> int:
        return self.assets[-1].ts_utc

    @property
    def duration_days(self) -> float:
        return (self.end_utc - self.start_utc) / 86400.0

    @property
    def photo_count(self) -> int:
        return len(self.assets)

    @property
    def total_distance_km(self) -> float:
        return sum(leg.distance_km for leg in self.legs)

    @property
    def asset_ids(self) -> list[str]:
        return [a.id for a in self.assets]

    @property
    def display_name(self) -> str:
        """What the album is actually called. Hand-edits always win."""
        return self.custom_name or self.heuristic_name
