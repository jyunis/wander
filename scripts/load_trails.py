"""Bulk-load hiking trails (OSM) into the store as a display-only overlay.

Foot/backcountry paths (highway=path|bridleway, named) + named hiking ROUTE
relations (e.g. the North Country Trail). They land in osm_features
(feature_class='trail'); the /trails overlay reads them LOCALLY — public Overpass
is far too flaky to depend on a live per-viewport query (see ingest/trails.py).
Never scored (TLC-3).

Covers a set of STATES (MN, WI, MI, SD, WY). Per state: hiking-route relations in
one query (a through-trail must come as one relation to keep its name — `out geom`
returns the relation's FULL geometry, so e.g. the NCT is fully captured) + named
paths fetched tiled. Resilient: each query tries several Overpass endpoints and
retries the whole set with backoff (Overpass 504/429s under load), so a rough
patch doesn't silently drop tiles. Rows are deduped by way id across states.

Two modes, chosen by whether STATES is set:
  - STATES=WY  -> APPEND: fetch just those states and UPSERT them into the stored
    set, leaving every other state untouched. Use this to add a new state or
    refresh one; it can't regress other states even if a tile drops.
  - (no STATES) -> REPLACE: rebuild the whole feature_class='trail' set from all
    states. Guarded: if Overpass dropped tiles AND the new set is smaller than
    what's stored, it refuses rather than regress live coverage.

Run in-image with /data mounted + internet (writes the live cairn.db):

    docker run --rm --network home-proxy -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data -e STATES=WY --entrypoint uv cairn:dev \
      run python scripts/load_trails.py

Env: STATES="WY" (append) or unset (full replace); OVERPASS_URLS="https://a/…,…"
to override endpoints (e.g. when the main instance is down).
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
    "WY": (-111.1, 40.9, -104.0, 45.1),  # Winds, Tetons, Bighorns, Cloud Peak, Snowies
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


QUERY_ROUNDS = 4
QUERY_BACKOFF_S = 20  # 20s, 40s, 60s between full-set retries


async def _query(client: httpx.AsyncClient, ql: str) -> list[dict] | None:
    """Run Overpass QL against the endpoints in order; first success wins. If every
    endpoint fails, back off and retry the whole set (QUERY_ROUNDS). Overpass
    routinely 504/429s under load; without this a transient rough patch skips whole
    tiles, and since the load REPLACES the full trail set those trails would be lost
    (regressing live coverage). Returns elements, or None only if all rounds fail."""
    for attempt in range(QUERY_ROUNDS):
        for url in ENDPOINTS:
            try:
                r = await client.get(url, params={"data": ql}, timeout=120.0)
                if r.status_code != 200:
                    print(f"  [{url}] HTTP {r.status_code}", flush=True)
                    continue
                return (r.json() or {}).get("elements", [])
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                print(f"  [{url}] {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        if attempt < QUERY_ROUNDS - 1:
            wait = QUERY_BACKOFF_S * (attempt + 1)
            print(f"  all endpoints failed — retrying in {wait}s "
                  f"(round {attempt + 2}/{QUERY_ROUNDS})", flush=True)
            await asyncio.sleep(wait)
    return None


def _box(w, s, e, n) -> str:
    return f"{s},{w},{n},{e}"  # Overpass order: south,west,north,east


async def fetch_state(client: httpx.AsyncClient, box: tuple) -> tuple[list[dict], int]:
    """Fetch a state's trail elements. Returns (elements, failed) where `failed` is
    the number of queries (routes + tiles) that came back empty after all retries —
    the caller uses it to refuse a coverage-regressing replace."""
    w0, s0, e0, n0 = box
    elements: list[dict] = []
    failed = 0

    ql = (
        "[out:json][timeout:180];"
        f'(relation["route"="hiking"]["name"]({_box(*box)}););out geom;'
    )
    routes = await _query(client, ql)
    if routes is None:
        failed += 1
        print("  routes query FAILED (all endpoints, all retries)", flush=True)
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
                failed += 1
                print(f"  tile {w:.0f},{s:.0f} FAILED — skipped", flush=True)
            else:
                elements += got
            lat += TILE_DEG
        lon += TILE_DEG
    return elements, failed


async def main() -> None:
    want = {s.strip().upper() for s in os.environ.get("STATES", "").split(",") if s.strip()}
    unknown = want - set(STATES)
    if unknown:
        raise SystemExit(f"Unknown STATES {sorted(unknown)}; known: {sorted(STATES)}")
    states = {k: v for k, v in STATES.items() if not want or k in want}
    # Targeting a subset (e.g. STATES=WY) APPENDS: upsert those states' trails into
    # the stored set without touching other states — safe to add or refresh one
    # state, and it can't regress others even if Overpass drops tiles. A full run
    # (no STATES) REPLACES the whole feature_class='trail' set (from-scratch rebuild).
    append = bool(want)
    mode = "append" if append else "replace"
    s = Settings(_env_file=None, data_dir=os.environ.get("CAIRN_DATA_DIR", "/data"))
    store = await Store.open(s)
    try:
        print(f"[trails] mode={mode} states={','.join(states)}", flush=True)
        merged: dict[int, tuple] = {}  # osm_id -> row (dedup across states; routes win)
        dropped = 0  # queries that failed after all retries (incomplete coverage)
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
            for name, box in states.items():
                print(f"[trails] === {name} ===", flush=True)
                els, failed = await fetch_state(client, box)
                dropped += failed
                for row in trail_rows(els):
                    prev = merged.get(row[1])
                    if prev is None or json.loads(row[3]).get("kind") == "hiking_route":
                        merged[row[1]] = row
                print(f"[trails] {name} done — {len(merged)} unique trails so far", flush=True)
        rows = list(merged.values())
        named = sum(1 for r in rows if json.loads(r[3]).get("name"))

        if append:
            # Upsert only — never delete, so other states stay intact. Dropped tiles
            # just mean fewer trails load this run; re-run to fill the gaps.
            await store.upsert_osm_chunk(rows)
            if dropped:
                print(f"[trails] note: {dropped} queries failed (Overpass flaky) — "
                      f"re-run STATES={','.join(states)} to fill any gaps.", flush=True)
            print(f"[trails] APPENDED {len(rows)} trail features ({named} named) for "
                  f"{','.join(states)} into {s.db_path}", flush=True)
        else:
            # Full rebuild: REPLACE the whole set. If Overpass dropped tiles AND the
            # new set is smaller than what's stored, refuse — replacing would REGRESS
            # live coverage. Re-run when Overpass is healthier (the retries help).
            cur = await store.db.execute(
                "SELECT COUNT(*) FROM osm_features WHERE feature_class = 'trail'"
            )
            existing = (await cur.fetchone())[0]
            if dropped and len(rows) < existing:
                print(f"[trails] ABORT: {dropped} queries failed and the new set "
                      f"({len(rows)}) is smaller than the {existing} stored — refusing to "
                      f"replace and regress coverage. Re-run when Overpass is healthier; "
                      f"existing trails are untouched.", flush=True)
                return
            if dropped:
                print(f"[trails] WARNING: {dropped} queries failed but new set "
                      f"({len(rows)}) >= {existing} stored — proceeding.", flush=True)
            n = await store.replace_trails(rows)
            print(f"[trails] REPLACED — stored {n} trail features ({named} named) for "
                  f"{','.join(states)} into {s.db_path}", flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
