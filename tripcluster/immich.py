"""Immich API client.

Immich's REST API changes between releases, so this module deliberately treats
endpoint paths as *configuration*, not constants, and ships a ``verify_spec``
check that compares the configured paths against the running instance's OpenAPI
document. Run ``cluster_trips.py --check-api`` against your instance before
trusting anything here, and pin the Immich version in docker-compose.yml.

Response parsing is intentionally tolerant: field names are looked up across the
shapes Immich has used, and anything unrecognised is skipped with a warning
rather than crashing a weekly cron job.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Iterator

import requests

from .config import ApiConfig
from .model import Asset

log = logging.getLogger(__name__)


class ImmichError(RuntimeError):
    pass


class ImmichClient:
    def __init__(self, cfg: ApiConfig):
        if not cfg.key:
            raise ImmichError("no API key: set IMMICH_API_KEY in the environment")
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"x-api-key": cfg.key, "Accept": "application/json"}
        )

    def _url(self, path: str, **fmt: Any) -> str:
        return self.base + path.format(**fmt)

    def _request(self, method: str, path: str, *, fmt: dict | None = None, **kw) -> Any:
        url = self._url(path, **(fmt or {}))
        resp = self.session.request(method, url, timeout=self.cfg.timeout_seconds, **kw)
        if resp.status_code >= 400:
            raise ImmichError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:400]}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ImmichError(f"{method} {url} returned non-JSON: {resp.text[:200]}")

    # ---- schema verification ---------------------------------------------

    def verify_spec(self) -> list[str]:
        """Compare configured endpoints against the live OpenAPI document.

        Returns a list of human-readable problems; empty means everything the
        clusterer calls exists on this instance.
        """
        try:
            spec = self._request("GET", self.cfg.openapi_spec)
        except ImmichError as e:
            return [
                f"could not fetch OpenAPI spec at {self.cfg.openapi_spec}: {e}. "
                "Find the right path (try /api/specs-json or the docs page) and "
                "set api.openapi_spec in config.yaml."
            ]

        paths: dict = spec.get("paths", {}) if isinstance(spec, dict) else {}
        if not paths:
            return [f"OpenAPI document at {self.cfg.openapi_spec} has no 'paths'"]

        # Normalise both sides: our {id} vs the spec's {albumId} etc.
        def norm(p: str) -> str:
            return re.sub(r"\{[^}]+\}", "{}", p)

        available = {norm(p): p for p in paths}
        problems: list[str] = []

        for label, path, method in (
            ("api.search_assets", self.cfg.search_assets, "post"),
            ("api.create_album", self.cfg.create_album, "post"),
            ("api.update_album", self.cfg.update_album, "patch"),
            ("api.add_album_assets", self.cfg.add_album_assets, "put"),
        ):
            key = norm(path)
            if key not in available:
                near = [p for p in available.values() if _tail(p) == _tail(path)]
                hint = f" Similar paths on this instance: {near}" if near else ""
                problems.append(f"{label}: {path} not found in spec.{hint}")
                continue
            ops = {m.lower() for m in paths[available[key]]}
            if method not in ops:
                problems.append(
                    f"{label}: {path} exists but has no {method.upper()} "
                    f"(has {sorted(ops)})"
                )

        return problems

    # ---- assets -----------------------------------------------------------

    def iter_assets(self, updated_after: str | None = None) -> Iterator[dict]:
        """Page through every asset, newest-first, yielding raw dicts."""
        page = 1
        while True:
            body: dict[str, Any] = {"page": page, "size": self.cfg.page_size}
            if updated_after:
                body["updatedAfter"] = updated_after
            data = self._request("POST", self.cfg.search_assets, json=body)

            items = _dig(data, ["assets", "items"]) or _dig(data, ["items"]) or []
            if not items:
                return
            yield from items

            next_page = _dig(data, ["assets", "nextPage"]) or _dig(data, ["nextPage"])
            if not next_page:
                return
            page = int(next_page)

    # ---- albums -----------------------------------------------------------

    def create_album(self, name: str, description: str, asset_ids: list[str]) -> str:
        data = self._request(
            "POST",
            self.cfg.create_album,
            json={
                "albumName": name,
                "description": description,
                # Immich caps album-create payloads; add the rest separately.
                "assetIds": asset_ids[:1000],
            },
        )
        album_id = (data or {}).get("id")
        if not album_id:
            raise ImmichError(f"album create returned no id: {str(data)[:200]}")
        if len(asset_ids) > 1000:
            self.add_assets(album_id, asset_ids[1000:])
        return album_id

    def add_assets(self, album_id: str, asset_ids: list[str]) -> int:
        added = 0
        for chunk in _chunks(asset_ids, 500):
            result = self._request(
                "PUT",
                self.cfg.add_album_assets,
                fmt={"id": album_id},
                json={"ids": chunk},
            )
            if isinstance(result, list):
                added += sum(1 for r in result if r.get("success"))
            else:
                added += len(chunk)
        return added

    def update_album(self, album_id: str, *, name: str | None = None,
                     description: str | None = None) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["albumName"] = name
        if description is not None:
            body["description"] = description
        if body:
            self._request("PATCH", self.cfg.update_album, fmt={"id": album_id}, json=body)


# ---- parsing --------------------------------------------------------------

_TS_FIELDS = ("dateTimeOriginal", "localDateTime", "fileCreatedAt", "createdAt")


def parse_asset(raw: dict) -> Asset | None:
    """Normalise one API asset dict, or None if it has no usable timestamp."""
    asset_id = raw.get("id")
    if not asset_id:
        return None

    exif = raw.get("exifInfo") or {}

    ts_raw = None
    for field in _TS_FIELDS:
        ts_raw = exif.get(field) or raw.get(field)
        if ts_raw:
            break
    dt = _parse_dt(ts_raw)
    if dt is None:
        log.debug("asset %s has no parseable timestamp", asset_id)
        return None

    lat = _first_float(exif, raw, ("latitude", "gpsLatitude"))
    lon = _first_float(exif, raw, ("longitude", "gpsLongitude"))

    return Asset(
        id=asset_id,
        ts_utc=int(dt.timestamp()),
        tz_offset_min=_tz_offset_min(exif, raw, dt),
        lat=lat,
        lon=lon,
    )


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A naive stamp is local wall time; treat as UTC and let the separately
        # reported offset (if any) carry the real timezone.
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tz_offset_min(exif: dict, raw: dict, dt: datetime) -> int | None:
    # Immich reports the zone as an IANA name or a "UTC+2" style string
    # depending on version; fall back to whatever the timestamp carried.
    tz = exif.get("timeZone") or raw.get("timeZone")
    if isinstance(tz, str) and tz:
        m = re.match(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$", tz.strip())
        if m:
            sign = 1 if m.group(1) == "+" else -1
            return sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))
        try:
            from zoneinfo import ZoneInfo

            off = dt.astimezone(ZoneInfo(tz)).utcoffset()
            if off is not None:
                return int(off.total_seconds() // 60)
        except Exception:
            pass
    off = dt.utcoffset()
    return int(off.total_seconds() // 60) if off is not None else None


def _first_float(*sources_and_keys) -> float | None:
    *sources, keys = sources_and_keys
    for src in sources:
        for key in keys:
            val = src.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    return None


def _dig(data: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _tail(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
