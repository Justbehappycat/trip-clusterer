# Trip Photo Wall — working context

Ambient touch photo display for road trips. A Mac Mini runs Immich + a slideshow
renderer; an MSI Cubi N100 on a touchscreen shows it in Edge kiosk mode. Full
build plan lives outside this repo.

This repo is **Phase 3 only** — the trip clusterer, the one piece written from
scratch. Everything else in the plan uses off-the-shelf software as-is.

See [README.md](README.md) for setup, CLI usage and threshold semantics. This
file is the state of the build.

## Where the build stands

**Immich runs on the Synology at `192.168.1.218:2283`, not the Mac Mini.** The
plan's "Mac Mini runs Immich + a slideshow renderer" is superseded. The Mini's
remaining role is undecided — immich-kiosk is itself a container and could sit
next to Immich on the Synology, in which case the Mini is not in the
architecture at all.

| Phase | Status |
|---|---|
| 1 — Immich on the Synology | **done** — v3.1.0 |
| 2 — Photo import, GPS verified | **in progress** — iPhone backing up, ~16K assets and climbing; GPS coverage not yet verified |
| 3 — Trip clusterer | **built, tested against synthetic data only** |
| 4 — immich-kiosk slideshow | not started |
| 5 — Windows kiosk hardening | not started |
| 6 — Touch handoff wrapper page | not started |

Phase 3 was developed on a MacBook Pro. The **endpoint paths are now confirmed
against the live 3.1.0 instance** — all four exist. `parse_asset`'s response
shapes and the whole `--apply` path are still verified only against the stub
server in `tests/test_immich.py`.

**Immich 3.1.0 publishes no OpenAPI document under `/api`.** Every candidate
path 404s (`/api/docs-json`, `/api/spec-json`, `/api/specs-json`,
`/api/swagger-json`, `/api/openapi.json`); `/openapi.json` returns 200 but it is
the web app's HTML catch-all, not a spec. So `verify_spec` falls back to
probing each configured endpoint. The probes go out **unauthenticated on
purpose**: Immich resolves the route before authenticating, so a live path
answers 401 and a moved one answers 404 — and an unauthenticated `POST
/api/albums` cannot create anything. Never make the probe send the API key;
`--check-api` would then litter the library with empty albums on every run.

Confirmed present on 3.1.0: `POST /api/search/metadata`, `POST /api/albums`,
`PATCH /api/albums/{id}`, `PUT /api/albums/{id}/assets`.

`/api/server/features` reports `smartSearch`, `facialRecognition`,
`duplicateDetection`, `map` and `reverseGeocoding` all true — the ML needed to
filter people out of the slideshow is available.

Remaining before the first real run, in this order:

1. Let the iPhone backup finish. Trip boundaries and `find_home.py`'s density
   estimate both shift while assets are still arriving.
2. Verify GPS coverage across the library. If location is being stripped,
   `segment_trips` silently filters everything out and returns zero trips —
   there is no error to notice.
3. `tools/find_home.py` to get real home coordinates into `config.yaml`.
4. Dry runs until the albums look right. Expect two or three rounds.
5. Only then `--apply`.

## Open defects

**Flight/drive classification — largely resolved, with two documented limits.**

Leg speed is measured last-photo-of-one-stop to first-photo-of-the-next, so it
absorbs airport time and understates the real pace by however long nobody was
shooting. Two tests now run, either sufficient to call a flight:

1. `flight_min_speed_kmh` (150, down from 300).
2. **Road feasibility** — the one that actually fires. Legs are great-circle;
   roads run ~1.25x longer. If covering that road distance in the elapsed time
   needs a sustained average above `max_sustained_road_kmh` (130), no car did
   it. Effective great-circle cutoff: 104 km/h.

Feasibility is the sounder test because it errs in the safe direction: elapsed
time overstates travel time, so the required road pace is a *lower bound* on
what really happened. Seattle→Denver with a 15-hour gap implies 109 km/h — an
ordinary driving pace no threshold could flag — but needs 137 km/h on road, so
it is now correctly a flight.

What it does **not** fix, both pinned in `tests/test_cluster.py`:

- **Short flights.** Seattle→Spokane with 5 hours of airport either side needs
  only 90 km/h on road. Genuinely indistinguishable from driving it.
  (`test_short_flight_with_a_long_gate_gap_is_still_a_known_miss`)
- **Very wide gaps.** For a 1,640 km leg the method reaches to about a 16-hour
  gap. The synthetic fixture's own Seattle→Denver has a 22-hour gap, implying
  75 km/h — that is an ordinary two-day drive and is *correctly* classified as
  driving. Not every miss is a defect.

The margin against real drives is thin and worth respecting:
`test_a_hard_but_real_drive_is_not_called_a_flight` (900 km in 9 h, 125 km/h on
road) is what breaks first if anyone lowers the ceiling or raises circuity.

Route evidence, which this note once called for, does not apply. An en-route
photo doesn't sit *inside* a leg: `cluster_stops` files every GPS-fixed photo
into a stop, so an intermediate photo **splits the leg in two**. A drive with
en-route photos already classifies correctly as a chain of short legs. The legs
that reach these tests are precisely the ones with nothing to consult.
`Asset.tz_offset_min` is also a dead end — Seattle→Denver crosses a timezone
whether flown or driven, so it only restates east-west displacement, which
`haversine_km` already gives.

Still measured against synthetic fixtures only. How often either limit matters
is a question for dry runs against the real library.

## Deliberate deviations from the plan

- `stop_gap_hours` defaults to **30**, not the plan's 12. At 12 an overnight with
  no photos opens a new stop, so a four-day stay in one city reads as four stops.
- Added `road_trip_min_ground_fraction`. At the default 1.0 it is the plan's
  strict "all long-haul legs are ground" rule; lowering it to ~0.5 also admits
  fly-out-then-drive trips, which the strict rule misses.
- Trips begin at the first *away* stop. If home is Los Angeles, an LA→NY drive
  names itself "Las Vegas → New York". Correct by definition, but not what the
  plan's example name implies.
- **`homes:` — more than one home, and homes with dates.** The plan assumes a
  single fixed point. The real library has three: Orange County, then Los
  Angeles after a move, plus a concurrent second home in Dalian. With one point
  every stay at an unmodelled home clusters and gets named as a trip, which is
  what produced dozens of "Dalian, CN" and "Orange, CA · 0 km travelled"
  albums in the first real dry run. A photo is away only when it is far from
  *every* home in effect on the day it was taken. `home:` still works and means
  one home, always in effect.

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
