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

**Flights between two away stops classify as ground travel.** Leg speed is
measured last-photo-of-one-stop to first-photo-of-the-next, so it absorbs
airport time. The fixture's Seattle→Denver flight implies ~110 km/h and the trip
is labelled a road trip. No threshold fixes this — 150 km/h was tested and
doesn't. The fix is to replace implied speed in `cluster.classify_legs` with
route evidence: a long drive leaves photos at intermediate points, a flight
leaves none. `tests/test_cluster.py::test_flight_threshold_is_sensitive_to_photo_gaps`
pins the current behaviour so it can't drift silently.

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
