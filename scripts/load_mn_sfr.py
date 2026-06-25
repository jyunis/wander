"""Bulk-load MN DNR State Forest Roads into the store as drivable ways.

MN *state* forest roads (DNR-administered access roads inside state forests) —
the road gap left by USFS MVUM (national-forest only) and OpenStreetMap. They
land as `drivable` osm_features, so `/roads` draws them and scoring treats them
as forest roads automatically (doc #34 follow-up; see ingest/mn_sfr.py).

Source: MN Geospatial Commons "MNDNR Administered State Forest Roads" ArcGIS
FeatureServer — ~1,632 statewide segments, native EPSG:26915, queried as 4326.
The whole statewide layer is small, so we bulk-preload it once rather than
fetching per-bbox (roads then show everywhere on browse, with no live fetch).

Run in-image with /data mounted + internet (writes the live cairn.db):

    docker run --rm --network home-proxy -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data --entrypoint uv cairn:dev \
      run python scripts/load_mn_sfr.py

Idempotent (keyed upsert) — safe to re-run to refresh after a DNR update.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from cairn.config import Settings
from cairn.ingest.mn_sfr import ingest_mn_sfr
from cairn.store import Store

FEATURESERVER = os.environ.get(
    "MN_SFR_URL",
    "https://enterprise.gisdata.mn.gov/aghost/rest/services/"
    "us_mn_state_dnr/trans_state_forest_roads/FeatureServer/0",
)
PAGE = 2000  # the service's maxRecordCount
OUT_FIELDS = "objectid,road_num,road_name,road_class"
UA = "wander-mn-sfr-loader (+http://wander.home.arpa)"


async def fetch_all() -> list[dict]:
    """Page the FeatureServer; return ``[{attributes, paths}]`` in lon/lat (4326)."""
    out: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=90.0, headers={"User-Agent": UA}) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": OUT_FIELDS,
                "returnGeometry": "true",
                "outSR": "4326",
                "geometryPrecision": "6",
                "resultOffset": str(offset),
                "resultRecordCount": str(PAGE),
                "f": "json",
            }
            r = await client.get(FEATURESERVER + "/query", params=params)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise SystemExit(f"ArcGIS error: {data['error']}")
            batch = data.get("features", [])
            for f in batch:
                paths = (f.get("geometry") or {}).get("paths") or []
                out.append({"attributes": f.get("attributes", {}), "paths": paths})
            print(f"[mn_sfr] fetched {len(out)} (offset {offset})", flush=True)
            if not batch or not data.get("exceededTransferLimit"):
                break
            offset += len(batch)
            if offset > 100_000:  # safety: never page unbounded
                print("[mn_sfr] paging safety cap hit", flush=True)
                break
    return out


async def main() -> None:
    s = Settings(_env_file=None, data_dir=os.environ.get("CAIRN_DATA_DIR", "/data"))
    store = await Store.open(s)
    try:
        features = await fetch_all()
        n = await ingest_mn_sfr(store, features, chunk_size=s.ingest_chunk_size)
        print(f"[mn_sfr] ingested {n} drivable ways into {s.db_path}", flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
