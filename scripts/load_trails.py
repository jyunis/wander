"""Bulk-load hiking trails (OSM) into the store as a display-only overlay.

Foot/backcountry paths (highway=path|bridleway, named) + named hiking ROUTE
relations (e.g. the North Country Trail). They land in osm_features
(feature_class='trail'); the /trails overlay reads them LOCALLY — public Overpass
is far too flaky to depend on a live per-viewport query (see ingest/trails.py).
Never scored (TLC-3).

Covers a set of STATES (default: MN, WI, MI, SD). Per state: hiking-route
relations in one query (a through-trail must come as one relation to keep its
name — `out geom` returns the relation's FULL geometry, so e.g. the NCT is fully
captured) + named paths fetched tiled. Resilient: tries several Overpass
endpoints per query and skips/logs a tile that fails. Rows are deduped by way id
across states (overlapping boxes are fine).

Run in-image with /data mounted + internet (writes the live cairn.db):

    docker run --rm --network home-proxy -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data --entrypoint uv cairn:dev \
      run python scripts/load_trails.py

Env: STATES="MN,WI" to subset; OVERPASS_URLS="https://a/api/interpreter,..." to
override endpoints (e.g. when the main instance is down). Re-run to refresh — it
replaces the whole feature_class='trail' set.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from cairn.config import Settings
from cairn.ingest.trails import trail_rows
from cairn.store import Store

# State bounding boxes (lightly padded), lon/lat: (west, south, east, north).
STATES: dict[str, tuple[float, float, float, float]] = {
    "MN": (-97.5, 43.4, -89.4, 49.5),
    "WI": (-93.0, 42.4, -86.7, 47.2),
    "MI": (-90.5, 41.6, -82.1, 48.4),
    "SD": (-104.2, 42.4, -96.4, 46.0),
}
TILE_DEG = 2.0  # named-path fetch tile size
UA = "wander-trails-loader (+http://wander.home.arpa)"

ENDPOINTS = [
    u.strip() for u in os.environ.get(
        "OVERPASS_URLS",
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter",
    ).split(",") if u.strip()
]


async def _query(client: httpx.AsyncClient, ql: str) -> list[dict] | None:
    """Run Overpass QL against the endpoints in order; first success wins.
    Returns elements, or None if every endpoint failed."""
    for url in ENDPOINTS:
        try:
            r = await client.get(url, params={"data": ql}, timeout=120.0)
            if r.status_code != 200:
                print(f"  [{url}] HTTP {r.status_code}", flush=True)
                continue
            return (r.json() or {}).get("elements", [])
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            print(f"  [{url}] {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    return None


def _box(w, s, e, n) -> str:
    return f"{s},{w},{n},{e}"  # Overpass order: south,west,north,east


async def fetch_state(client: httpx.AsyncClient, box: tuple) -> list[dict]:
    w0, s0, e0, n0 = box
    elements: list[dict] = []

    ql = (
        "[out:json][timeout:180];"
        f'(relation["route"="hiking"]["name"]({_box(*box)}););out geom;'
    )
    routes = await _query(client, ql)
    if routes is None:
        print("  routes query FAILED (all endpoints)", flush=True)
    else:
        elements += routes
        print(f"  routes: {len(routes)} relation(s)", flush=True)

    lon = w0
    while lon < e0:
        lat = s0
        while lat < n0:
            w, s = lon, lat
            e, n = min(lon + TILE_DEG, e0), min(lat + TILE_DEG, n0)
            ql = (
                "[out:json][timeout:120];"
                f'(way["highway"~"^(path|bridleway)$"]["name"]({_box(w, s, e, n)}););out geom;'
            )
            got = await _query(client, ql)
            if got is None:
                print(f"  tile {w:.0f},{s:.0f} FAILED — skipped", flush=True)
            else:
                elements += got
            lat += TILE_DEG
        lon += TILE_DEG
    return elements


async def main() -> None:
    want = {s.strip().upper() for s in os.environ.get("STATES", "").split(",") if s.strip()}
    states = {k: v for k, v in STATES.items() if not want or k in want}
    s = Settings(_env_file=None, data_dir=os.environ.get("CAIRN_DATA_DIR", "/data"))
    store = await Store.open(s)
    try:
        merged: dict[int, tuple] = {}  # osm_id -> row (dedup across states; routes win)
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
            for name, box in states.items():
                print(f"[trails] === {name} ===", flush=True)
                for row in trail_rows(await fetch_state(client, box)):
                    prev = merged.get(row[1])
                    if prev is None or json.loads(row[3]).get("kind") == "hiking_route":
                        merged[row[1]] = row
                print(f"[trails] {name} done — {len(merged)} unique trails so far", flush=True)
        rows = list(merged.values())
        n = await store.replace_trails(rows)
        named = sum(1 for r in rows if json.loads(r[3]).get("name"))
        print(f"[trails] stored {n} trail features ({named} named) "
              f"for {','.join(states)} into {s.db_path}", flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
