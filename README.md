# trip-clusterer

Turns an Immich library into trip albums. Phase 3 of the Trip Photo Wall plan —
the only piece written from scratch, because no photo app detects road trips well.

Reads assets from Immich, segments them into trips, clusters stops, classifies
the travel between them, names the result, and creates or updates one album per
trip. State lives in SQLite so weekly reruns are cheap and idempotent.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Fill in `home:` — derive it from the library rather than guessing:

```bash
.venv/bin/python tools/find_home.py --config config.yaml
```

The API key is never stored in `config.yaml`:

```bash
export IMMICH_API_KEY=...
export IMMICH_BASE_URL=http://<mini>:2283
```

## Before the first real run

Immich's REST API moves between releases, so the endpoint paths in
`config.yaml` are configuration, not constants. Check them against your actual
instance and pin the Immich version in `docker-compose.yml`:

```bash
.venv/bin/python cluster_trips.py --check-api
```

It fetches the instance's OpenAPI document, verifies every path and method the
clusterer uses, and suggests near-matches for anything that moved.

## Running

Nothing is written unless you pass `--apply`.

```bash
.venv/bin/python cluster_trips.py                 # dry run: prints proposed albums
.venv/bin/python cluster_trips.py --apply         # create/update albums
.venv/bin/python cluster_trips.py --no-fetch      # re-cluster the cache, no API calls
.venv/bin/python cluster_trips.py --list          # recorded trips and album ids
.venv/bin/python cluster_trips.py --rename 4 "The Big One"
```

`--rename` sets a `custom_name` that reruns never overwrite. The heuristic name
keeps updating underneath it, but the album keeps the name you gave it.

## Tuning without Immich

`--from-json` clusters a plain JSON asset list, so thresholds can be tuned
before the library exists:

```bash
.venv/bin/python tools/make_fixtures.py > fixtures/synthetic.json
.venv/bin/python cluster_trips.py --from-json fixtures/synthetic.json
```

The synthetic library contains a year of home photos, a 9-day LA→NY drive, a
Seattle trip with a flight to Denver mid-way, a one-day Joshua Tree run that
should be rejected, and a stretch of the drive with GPS stripped.

## What the thresholds actually do

All in `config.yaml`. Starting points, not answers.

| Threshold | Effect |
|---|---|
| `home_radius_km` (40) | Further than this from home = "away". Note the first stop of every trip is the first *away* stop — if you live in LA, an LA→NY drive is named "Las Vegas → New York". |
| `max_gap_hours` (36) | A longer gap between away photos splits one trip into two. The single biggest lever on results. |
| `min_duration_days` (2), `min_photos` (10) | Reject day trips and thin runs. |
| `stop_gap_hours` (30) | Deliberately higher than the plan's 12. At 12, an overnight with no photos splits a four-day stay in one city into four separate stops, and the description reads as a list of days rather than places. |
| `flight_min_speed_kmh` (150) | Above this implied speed a leg is a flight. Lowered from 300; see the caveat below for what it still misses. |
| `road_trip_min_ground_fraction` (1.0) | Share of long-haul km that must be on the ground. 1.0 is the strict "no flights" rule; ~0.5 also admits fly-out-then-drive trips. |

### Caveat: implied speed is a weak flight detector

Leg speed is measured from one stop's last photo to the next stop's first
photo, so it includes airport time and any stretch where nobody shot anything.
It therefore always *understates* how fast you were really moving, and how much
it understates depends entirely on when you happened to take pictures.

How far the same Seattle→Denver flight (~1,640 km) drifts with the photo gap:

| Photo gap | Implied speed | Road pace it needs | Verdict |
|---|---|---|---|
| 7 h | ~234 km/h | 293 km/h | `flight` ✓ |
| 15 h | ~109 km/h | 137 km/h | `flight` ✓ |
| 22 h | ~75 km/h | 93 km/h | `driving` — and rightly so |

The second row is the one a speed threshold could never reach: 109 km/h *is* a
driving pace, so no `flight_min_speed_kmh` low enough to catch it leaves real
road trips alone. What catches it is a different question — **not "was this
fast?" but "could a car have covered this ground in this time?"**

Legs are measured great-circle; roads run about 1.25x longer. 1,640 km straight
line is ~2,050 km of road, and 2,050 km in 15 hours demands a sustained
137 km/h. No car does that. The test is sound in the conservative direction:
elapsed time runs from one stop's last photo to the next stop's first, so it
*overstates* travel time, which makes the required pace a lower bound on what
really happened.

The third row is not a failure. 1,640 km in 22 hours is an ordinary two-day
drive, and nothing in the photo track distinguishes it from one. For a leg this
long the method reaches to roughly a 16-hour gap; past that the evidence is
genuinely gone.

Two limits worth knowing:

- **Short flights still read as drives.** Seattle→Spokane with 5 hours of
  airport either side implies 72 km/h and needs only 90 km/h on road — a
  perfectly possible drive. Pinned by
  `test_short_flight_with_a_long_gate_gap_is_still_a_known_miss`.
- **The margin against hard drives is thin.** The cutoff sits at 104 km/h
  great-circle; 900 km in 9 hours needs 125 km/h on road and stays `driving`,
  but 900 km in 8 hours does not. That case is pinned by
  `test_a_hard_but_real_drive_is_not_called_a_flight`, which is what breaks
  first if you lower the ceiling or raise circuity.

Route evidence is the usual suggested fix — a long drive leaves photos at
intermediate points, a flight leaves none — but note what it can and cannot do
here. An en-route photo does not sit *inside* a leg: `cluster_stops` files every
GPS-fixed photo into a stop, so an intermediate photo **splits the leg in two**.
A drive with en-route photos is therefore already classified correctly, as a
chain of short legs, without consulting speed at all. The legs that reach the
speed test are exactly the ones with no intermediate photos to consult. Any real
improvement has to come from evidence other than the photo track — timezone
shifts across the leg are the most promising, since a flight usually crosses one
and the EXIF offset is already parsed into `Asset.tz_offset_min`.

## Weekly run

See `launchd/com.tripcluster.weekly.plist` — edit the paths and the API key,
copy to `~/Library/LaunchAgents/`, then `launchctl load`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

No Immich instance required; everything below the API client is pure functions
over synthetic tracks.

## Layout

```
cluster_trips.py          CLI: fetch, cluster, dry-run, apply
tripcluster/
  config.py               config.yaml + env; thresholds
  model.py                Asset, Stop, Leg, Trip
  geo.py                  great-circle maths, GPS interpolation
  cluster.py              segmentation, stops, legs, road-trip test
  geocode.py              offline reverse geocoding of stop centroids
  naming.py               album names and descriptions
  store.py                SQLite: asset cache, trip↔album mapping, custom names
  immich.py               API client + OpenAPI verification
tools/
  find_home.py            derive home coordinates from the library
  make_fixtures.py        synthetic library for threshold tuning
```
