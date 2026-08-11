"""SQLite state so reruns are idempotent and cheap.

Two jobs:

1. Cache asset EXIF, so a rerun only fetches what's new.
2. Remember which computed trip maps to which Immich album — and to any
   ``custom_name`` typed by hand, which is never overwritten.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .model import Asset, Trip

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    ts_utc        INTEGER NOT NULL,
    tz_offset_min INTEGER,
    lat           REAL,
    lon           REAL,
    fetched_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_ts ON assets (ts_utc);

CREATE TABLE IF NOT EXISTS trips (
    trip_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id       TEXT,
    heuristic_name TEXT NOT NULL,
    custom_name    TEXT,
    kind           TEXT NOT NULL,
    start_utc      INTEGER NOT NULL,
    end_utc        INTEGER NOT NULL,
    description    TEXT,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_assets (
    trip_id  INTEGER NOT NULL REFERENCES trips (trip_id) ON DELETE CASCADE,
    asset_id TEXT    NOT NULL,
    PRIMARY KEY (trip_id, asset_id)
);
CREATE INDEX IF NOT EXISTS trip_assets_asset ON trip_assets (asset_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- assets -----------------------------------------------------------

    def upsert_assets(self, assets: list[Asset]) -> int:
        now = int(time.time())
        rows = [
            (a.id, a.ts_utc, a.tz_offset_min, a.lat, a.lon, now)
            for a in assets
            # Interpolated coordinates are derived, not source data - never
            # write them back into the cache or they compound on rerun.
            if not a.inferred
        ]
        self.db.executemany(
            "INSERT INTO assets (id, ts_utc, tz_offset_min, lat, lon, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts_utc=excluded.ts_utc, "
            "  tz_offset_min=excluded.tz_offset_min, lat=excluded.lat, "
            "  lon=excluded.lon, fetched_at=excluded.fetched_at",
            rows,
        )
        self.db.commit()
        return len(rows)

    def all_assets(self) -> list[Asset]:
        cur = self.db.execute(
            "SELECT id, ts_utc, tz_offset_min, lat, lon FROM assets ORDER BY ts_utc"
        )
        return [
            Asset(
                id=r["id"],
                ts_utc=r["ts_utc"],
                tz_offset_min=r["tz_offset_min"],
                lat=r["lat"],
                lon=r["lon"],
            )
            for r in cur
        ]

    def known_asset_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM assets")}

    # ---- trips ------------------------------------------------------------

    def match_trip(self, trip: Trip) -> sqlite3.Row | None:
        """Find the stored trip this computed trip is a continuation of.

        Matching on shared asset IDs rather than dates: a trip's date span can
        grow when late-imported photos extend it, but its photos don't change
        identity. The stored trip sharing the most assets wins.
        """
        ids = trip.asset_ids
        if not ids:
            return None
        placeholders = ",".join("?" * len(ids))
        row = self.db.execute(
            f"SELECT trip_id, COUNT(*) AS shared FROM trip_assets "
            f"WHERE asset_id IN ({placeholders}) "
            f"GROUP BY trip_id ORDER BY shared DESC LIMIT 1",
            ids,
        ).fetchone()
        if not row:
            return None
        return self.db.execute(
            "SELECT * FROM trips WHERE trip_id = ?", (row["trip_id"],)
        ).fetchone()

    def hydrate(self, trip: Trip) -> None:
        """Attach stored album_id / custom_name / trip_id to a computed trip."""
        row = self.match_trip(trip)
        if row is None:
            return
        trip.trip_id = row["trip_id"]
        trip.album_id = row["album_id"]
        trip.custom_name = row["custom_name"]

    def new_asset_ids(self, trip: Trip) -> list[str]:
        """Assets in this trip not yet recorded against its stored album."""
        if trip.trip_id is None:
            return trip.asset_ids
        known = {
            r[0]
            for r in self.db.execute(
                "SELECT asset_id FROM trip_assets WHERE trip_id = ?", (trip.trip_id,)
            )
        }
        return [aid for aid in trip.asset_ids if aid not in known]

    def save_trip(self, trip: Trip, description: str) -> int:
        """Persist a trip and its membership. Never touches ``custom_name``."""
        now = int(time.time())
        if trip.trip_id is None:
            cur = self.db.execute(
                "INSERT INTO trips (album_id, heuristic_name, custom_name, kind, "
                "  start_utc, end_utc, description, updated_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (trip.album_id, trip.heuristic_name, trip.kind,
                 trip.start_utc, trip.end_utc, description, now),
            )
            trip.trip_id = int(cur.lastrowid)
        else:
            self.db.execute(
                "UPDATE trips SET album_id = ?, heuristic_name = ?, kind = ?, "
                "  start_utc = ?, end_utc = ?, description = ?, updated_at = ? "
                "WHERE trip_id = ?",
                (trip.album_id, trip.heuristic_name, trip.kind, trip.start_utc,
                 trip.end_utc, description, now, trip.trip_id),
            )

        self.db.executemany(
            "INSERT OR IGNORE INTO trip_assets (trip_id, asset_id) VALUES (?, ?)",
            [(trip.trip_id, aid) for aid in trip.asset_ids],
        )
        self.db.commit()
        return trip.trip_id

    def set_custom_name(self, trip_id: int, name: str | None) -> None:
        self.db.execute(
            "UPDATE trips SET custom_name = ?, updated_at = ? WHERE trip_id = ?",
            (name, int(time.time()), trip_id),
        )
        self.db.commit()

    def list_trips(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM trips ORDER BY start_utc"))

    # ---- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()
