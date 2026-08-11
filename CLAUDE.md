# Trip Photo Wall — working context

Ambient touch photo display for road trips. A Mac Mini runs Immich + a slideshow
renderer; an MSI Cubi N100 on a touchscreen shows it in Edge kiosk mode. Full
build plan lives outside this repo.

This repo is **Phase 3 only** — the trip clusterer, the one piece written from
scratch. Everything else in the plan uses off-the-shelf software as-is.

See [README.md](README.md) for setup, CLI usage and threshold semantics. This
file is the state of the build.

## Where the build stands

| Phase | Status |
|---|---|
| 1 — Immich on the Mini | not started |
| 2 — Photo import, GPS verified | not started |
| 3 — Trip clusterer | **built, tested against synthetic data only** |
| 4 — immich-kiosk slideshow | not started |
| 5 — Windows kiosk hardening | not started |
| 6 — Touch handoff wrapper page | not started |

Phase 3 was developed on a MacBook Pro, not the Mini. It has **never talked to a
real Immich instance**. The endpoint paths, the response shapes in
`immich.py:parse_asset`, and the whole `--apply` path are verified only against
the stub server in `tests/test_immich.py`.

Before the first real run, in this order:

1. `--check-api` against the live instance; fix any paths it reports.
2. `tools/find_home.py` to get real home coordinates into `config.yaml`.
3. Dry runs until the albums look right. Expect two or three rounds.
4. Only then `--apply`.

## Open defects

**Flights between two away stops can still classify as ground travel — partly
fixed.** Leg speed is measured last-photo-of-one-stop to first-photo-of-the-next,
so it absorbs airport time and understates the real pace by however long nobody
was shooting.

`flight_min_speed_kmh` now defaults to **150**, not 300. That fixes the
moderate-gap case (7-hour gap, ~234 km/h implied, previously a road trip) and
breaks nothing else in the suite. It does **not** fix the wide-gap case: a
15-hour gap implies ~109 km/h, an ordinary driving pace, and no threshold
recovers it — going below 109 would start calling real road trips flights. 150
is the floor, since it already equals `drive_max_speed_kmh`.

Both cases are pinned in `tests/test_cluster.py`:
`test_moderate_photo_gap_flight_is_caught_at_the_default` (fixed, also guards
against someone restoring 300) and
`test_wide_photo_gap_flight_is_a_known_miss_at_any_threshold` (deliberately
asserts the wrong answer, so the limitation stays visible).

Route evidence — the fix this note used to call for — turns out not to apply.
An en-route photo doesn't sit *inside* a leg: `cluster_stops` files every
GPS-fixed photo into a stop, so an intermediate photo **splits the leg in two**.
A drive with en-route photos already classifies correctly as a chain of short
legs. The legs that reach the speed test are precisely the ones with no
intermediate photos to consult, so there is nothing there to look at. The next
candidate is `Asset.tz_offset_min`: a flight usually crosses a timezone, a drive
of the same length usually doesn't, and the EXIF offset is already parsed.

All of this is measured against synthetic fixtures only. Whether the wide-gap
miss matters at all depends on how often it happens in the real library —
settle that with dry runs after Phase 2, not by tuning further now.

## Deliberate deviations from the plan

- `stop_gap_hours` defaults to **30**, not the plan's 12. At 12 an overnight with
  no photos opens a new stop, so a four-day stay in one city reads as four stops.
- Added `road_trip_min_ground_fraction`. At the default 1.0 it is the plan's
  strict "all long-haul legs are ground" rule; lowering it to ~0.5 also admits
  fly-out-then-drive trips, which the strict rule misses.
- Trips begin at the first *away* stop. If home is Los Angeles, an LA→NY drive
  names itself "Las Vegas → New York". Correct by definition, but not what the
  plan's example name implies.

## Still unknown

The plan's inventory table is unfilled. Needed before Phases 1–2:

- Mac Mini model/chip, LAN IP or `.local` hostname
- Photo storage target: Mini internal SSD vs the Synology at `192.168.1.218`
- Cubi LAN IP; monitor resolution and orientation
- Real home coordinates, photo count, date range of trips

## Conventions

- Nothing is written to Immich without `--apply`. Dry run is the default; keep it
  that way.
- `custom_name` in `trips.db` is hand-typed and is **never** overwritten by a
  rerun. Any code that touches album naming must preserve this.
- Immich's REST API moves between releases. Endpoint paths are configuration in
  `config.yaml`, not constants — don't hardcode them, and pin the Immich version
  in `docker-compose.yml`.
- `config.yaml` and `trips.db` are gitignored. The API key comes from
  `IMMICH_API_KEY` in the environment and must never be committed.
- Everything below `immich.py` is pure functions over `Asset` lists — no network,
  no database. Keep it that way so the tests stay fast and instance-free.
- `.venv` is not relocatable (`reverse_geocoder` pulls numpy/scipy). Rebuild it
  from `requirements.txt` on a new machine rather than copying it.
