# Cairn 🪨

A campsite-discovery **scorer** for a teardrop camper. Given a bounding box and
a set of feature weights, Cairn derives **drive-to-able dispersed-campsite
candidates** from the land itself — terrain, hydrology, roads, tree cover — and
ranks them by physical suitability via multi-criteria analysis (MCDA) over
OpenStreetMap (Overpass) and AWS Terrarium elevation tiles.

> **Physical suitability only.** Cairn does **not** verify land ownership or
> camping legality. It never asserts a site is legal to camp — confirm that
> yourself before you go (TLC-3).

Design is the source of truth: see the *Cairn — Architecture* (#24) and
*Cairn — Contracts* (#25) documents in the `documents` MCP under
`/projects/cairn/`.

## Stack

- **Single container**, port **9931**, on the `home-proxy` network.
- **Python 3.12** + FastAPI/uvicorn, `uv`-managed.
- **SQLite + SpatiaLite** in-process (`data/cairn.db`, WAL) — no database
  container; near-zero steady-state memory footprint.
- Public, keyless data sources: Overpass + Terrarium.
- Static **Leaflet** UI (no build step), served by the API.

## Layout

```
src/cairn/
  config.py        Pydantic BaseSettings (caps, weights, zoom, LRU cap, ...)
  errors.py        typed error taxonomy
  api.py           FastAPI app
  main.py          uvicorn entry point (console script: cairn)
  store/           SQLite + SpatiaLite (schema.sql, repo.py, types.py)
  engine/          candidates -> features -> scoring (pure core) + scoring_config
  clients/         overpass, terrain (LRU slope sampler), cache
  ingest/          streamed OSM + slope ingest
  web/             static Leaflet single-page app
tests/             unit / integration / fakes
```

## Run

```sh
cp .env.example .env          # safe defaults; an empty .env also boots
docker compose up --build -d
curl -s localhost:9931/health | jq
```

`./data` (the SQLite DB + the disposable tile / OSM caches) is a mounted
volume. `data/tiles` and `data/osm-cache` are excluded from backup.

## Tests

Tests need the SpatiaLite extension, so run them in the image:

```sh
docker build -t cairn:dev .
docker run --rm -v "$PWD/tests:/app/tests" --entrypoint uv cairn:dev \
    run --extra dev pytest -q
```

## Version control

Working repo here; bare mirror at `~/repos/cairn.git` is `origin`. Build on
`feature/*` branches, one commit per milestone, merge to `master` when a
milestone passes.
