"""Album names and descriptions.

Names are *heuristic* — stored separately from any hand-edited ``custom_name``,
which the pipeline never overwrites.
"""

from __future__ import annotations

from datetime import datetime

from .cluster import farthest_stop
from .model import Trip


def _month_span(start: datetime, end: datetime) -> str:
    """'Sept 2023', 'Sept-Oct 2023', or 'Dec 2023-Jan 2024'."""
    s, e = start.strftime("%b"), end.strftime("%b")
    if start.year != end.year:
        return f"{s} {start.year}-{e} {end.year}"
    if s != e:
        return f"{s}-{e} {start.year}"
    return f"{s} {start.year}"


def heuristic_name(trip: Trip, prefix: str = "") -> str:
    start = trip.assets[0].local_dt
    end = trip.assets[-1].local_dt
    when = _month_span(start, end)

    if not trip.stops:
        name = f"Trip · {when}"
    elif trip.kind == "road_trip":
        origin = trip.stops[0]
        far = farthest_stop(trip.stops)
        if far is None or far is origin or far.label == origin.label:
            name = f"{origin.label} · {when}"
        else:
            name = f"{origin.label} → {far.label} · {when}"
    else:
        # Name a non-road trip after wherever the most photos were taken.
        main = max(trip.stops, key=lambda s: s.photo_count)
        name = f"{main.label} · {when}"

    return f"{prefix}{name}" if prefix else name


def describe(trip: Trip) -> str:
    """Compact album description: span, duration, stops, distance, itinerary."""
    start = trip.assets[0].local_dt
    end = trip.assets[-1].local_dt
    days = max(1, round(trip.duration_days))

    lines = [
        f"{start:%b %-d, %Y} – {end:%b %-d, %Y}  ·  {days} day{'s' if days != 1 else ''}"
        f"  ·  {len(trip.stops)} stop{'s' if len(trip.stops) != 1 else ''}"
        f"  ·  {trip.photo_count} photos",
    ]
    if trip.total_distance_km:
        lines.append(f"{trip.total_distance_km:,.0f} km travelled"
                     + ("  ·  road trip" if trip.kind == "road_trip" else ""))
    if trip.stops:
        # Date each stop in its own local time, not the machine's timezone.
        by_id = {a.id: a for a in trip.assets}
        lines.append("")
        for stop in trip.stops:
            first = by_id.get(stop.asset_ids[0]) if stop.asset_ids else None
            when = first.local_dt.strftime("%b %-d") if first else ""
            lines.append(f"  {when} — {stop.label} ({stop.photo_count} photos)")
    return "\n".join(lines)
