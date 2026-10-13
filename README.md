# Wander 🪨

Wander finds dispersed campsites you can drive to (and some you can't). You
draw a box over public land, and it scores the terrain, water, and roads to
rank candidate spots. Once you've picked a few, you can download the area so you
can navigate there with no cell signal.

<img width="700" alt="Wander Screenshot" src="https://github.com/user-attachments/assets/db62eabb-03e2-4455-a55f-9ef4552f6daf" />

It's a hobby project I've used to plan actual camping trips.

> **Physical suitability only.** Wander does **not** verify land ownership or
> whether camping is legal. It never claims a site is legal to camp, so check
> that yourself before you go. This rule is enforced in code: the ownership,
> mud, and cell-coverage overlays are display-only, and a test guards that the
> scoring engine never reads them.

(The Python package is still named `cairn`, the project's original name.)

## How the scoring works

1. **Candidates come from the land.** OpenStreetMap roads and
   tracks are pulled from Overpass, then supplemented with USFS MVUM forest
   roads, which OSM often lacks. The candidates are points along those roads,
   with extra weight on spur ends ("drive to the end of the road").
2. **Features.** Each candidate gets a set of normalized features: slope from
   AWS Terrarium elevation tiles (anything over 15° is dropped), distance to
   water, road character (forest-road class), road-network remoteness,
   solitude, and trail proximity. Each feature maps to `[0, 1]` through a shape
   defined as data (trapezoid, decay, linear), not code.
3. **Weighted ranking** Weights and presets are versioned in the
   database. Every run records which scoring-config version produced it, and a
   change adds a new version rather than editing an old one, so old rankings
   stay explainable.

## Keeping it honest: the eval set

[`eval/`](eval/README.md) holds ground truth: 76 sites from a public campsite
guide, plus sites I've verified in person. `validate.py` reports where the
known-good sites land in a run's ranking.

## Wander Field: plan online, navigate offline

Wander Field (`/field/`) is a separate, stripped-down MapLibre PWA for the
car. You draw a box, the server bakes a vector-basemap
[PMTiles](https://protomaps.com/) archive for it, and the phone caches the
archive, your saved pins, roads, and land-ownership polygons. From then on
the map, GPS, and "whose land am I on?" all work with no signal. A service
worker handles versioned updates and never reloads the page in the middle of
a download.

## Everything else

- **Overlays:** national, state, and county forest and park boundaries,
  wilderness areas (no vehicles), MVUM roads with private roads flagged,
  trails, graded cell coverage, and a road mud-susceptibility tier (soil
  drainage + slope + water + surface), recolored after recent rain.
- **Trip planning:** saved places, collections with AND filtering, trips with
  GPX and KML export, and state-park campground availability checks.
- **Ops:** SQLite + SpatiaLite in-process (WAL), memory caps on tile and
  ingest LRUs, wall-clock and bbox-area limits, a disposable tile and OSM
  cache, and an nginx satellite-tile cache with the API key kept server-side
  (`deploy/`).

## Stack

Python 3.12, FastAPI, SQLite + SpatiaLite, numpy, Leaflet (main app) and
MapLibre + PMTiles (field app). Neither front end has a build step. Data
sources are Overpass, AWS Terrarium, USFS MVUM, RIDB, and Protomaps; all are
public, and only the optional satellite tiles need a key.

## Run

```sh
cp .env.example .env          # safe defaults; an empty .env also boots
docker compose up --build -d
curl -s localhost:9931/health | jq
```

## Tests

The tests need the SpatiaLite extension, so run them in the image:

```sh
docker build -t wander:dev .
docker run --rm -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" \
    -v "$PWD/eval:/app/eval" --entrypoint uv wander:dev run --extra dev pytest -q
```

## License

MIT
