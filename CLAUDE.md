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
| 2 — Photo import, GPS verified | **done** — 17,283 timeline assets, 8,486 with GPS (~51%) |
| 3 — Trip clusterer | **applied** — 16 albums created in Immich, 8 labelled road trips |
| 4 — immich-kiosk slideshow | **running** on the Synology, port 3000, Kiosk 0.42.0 — verified serving real photos from the Views album |
| 5 — Windows kiosk hardening | **written, untested** — `deploy/phase-5-windows-kiosk.md` |
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

Everything below `immich.py` has now been calibrated against the real library.
Three defects only that data could expose, in the order they were found:

1. **`withExif` was never sent.** 3.1.0 omits the whole exifInfo block unless
   asked, so every asset arrived with a timestamp and no coordinates, and the
   pipeline would have found zero trips without raising anything.
2. **Live Photo video halves were counted as photos.** 23% of the library is
   `type=VIDEO visibility=hidden`; Immich excludes them from album counts, so
   173 photos sent read as 110 on the server.
3. **Legs measured centroid-to-centroid.** See below.

`tools/gps_audit.py`, `tools/inspect_asset_shape.py` and
`tools/inspect_outliers.py` exist because of these — all read-only, all safe to
re-run when something looks wrong.

## Open defects

**Flight/drive classification — largely resolved, with two documented limits.**

Leg speed is measured last-photo-of-one-stop to first-photo-of-the-next, so it
absorbs airport time and understates the real pace by however long nobody was
shooting. Two tests now run, either sufficient to call a flight:

1. `flight_min_speed_kmh` (150, down from 300).
2. **Road feasibility** — the one that actually fires. Legs are great-circle;
   roads run ~1.15x longer, and driving is not sustainable indefinitely:
   `drive_stint_hours` (8) at the wheel, then `drive_duty_cycle` (0.55) of the
   rest. If the distance exceeds what a car could cover in that drivable time
   at `max_sustained_road_kmh` (150), no car did it.

The duty cycle is the part that matters, and it took real data to see. A fixed
ceiling cannot separate 110 km/h for two hours (a brisk interstate run) from
110 km/h for fifteen (nobody) — it has to reject the first or accept the
second. Calibrating against synthetic fixtures gave 46 false flight legs on the
real library, because no fixture contained interstate driving. With the duty
cycle, the seven genuine cross-country drives all label correctly and the China
sea-crossings do not.

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

**Legs are measured between boundary photos, not stop centroids.** The
centroid answers "where is this stop" — right for naming and map pins, wrong
for measuring travel. A stop may span `stop_radius_km`, so two centroids can
sit 90 km apart while the photos bracketing the drive between them are minutes
and 15 km apart. Pairing centroid distance with photo timing manufactured
impossible speeds on exactly the short interstate legs inside real road trips;
`Stop.start_point`/`end_point` fix it, falling back to the centroid when a stop
has no measured fixes.

Route evidence, which this note once called for, does not apply. An en-route
photo doesn't sit *inside* a leg: `cluster_stops` files every GPS-fixed photo
into a stop, so an intermediate photo **splits the leg in two**. A drive with
en-route photos already classifies correctly as a chain of short legs. The legs
that reach these tests are precisely the ones with nothing to consult.
`Asset.tz_offset_min` is also a dead end — Seattle→Denver crosses a timezone
whether flown or driven, so it only restates east-west displacement, which
`haversine_km` already gives.

Calibrated against the real library, not fixtures. The synthetic suite alone
gave 46 false flight legs, because no fixture contained interstate driving.

## Deliberate deviations from the plan

- `stop_gap_hours` defaults to **30**, not the plan's 12. At 12 an overnight with
  no photos opens a new stop, so a four-day stay in one city reads as four stops.
- Added `road_trip_min_ground_fraction`, now **0.75**, not the plan's strict
  "all long-haul legs are ground" rule. At 1.0 a single misclassified leg out
  of forty flipped an obvious cross-country drive to a plain trip.
- Trips begin at the first *away* stop. If home is Los Angeles, an LA→NY drive
  names itself "Las Vegas → New York". Correct by definition, but not what the
  plan's example name implies.
- **A curated `Views` album feeds the wall, not the trip albums.** Album
  `ce8df127-f644-459a-b39a-5cc41c8d3a75`, built by
  `tools/build_views_album.py`: 722 landscape stills from trip photos with no
  detected face. The trip albums answer "where did I go"; a wall needs "is
  this worth looking at from across the room". 65% of the library is portrait
  and would pillarbox on the landscape screen, which is the biggest single
  filter. Rebuild the album after new trips land.
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
