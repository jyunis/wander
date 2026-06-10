"""USA Structures building-footprint client (FEMA / Oak Ridge).

National building footprints — including the rural cabins/homes OpenStreetMap
frequently doesn't have (the gap behind "pins on top of houses"). Public ArcGIS
REST service, no key (TLC-12: read-only GET). Cached, with backoff, paged.

Returns building polygons (rings) + occupancy class, so ingest can avoid
dwellings/commercial structures while skipping utility/agriculture (a vault
toilet at a boat landing shouldn't kill the site). Non-fatal: the run degrades
to OSM-only buildings if the service is down.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass

import httpx

from cairn.clients.cache import DiskCache
from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import StructuresBadResponse, StructuresUnreachable
from cairn.store.types import Bbox

log = logging.getLogger("cairn.structures")

QUERY_VERSION = 1
_OUT_FIELDS = "OBJECTID,OCC_CLS"


@dataclass
class StructuresResult:
    features: list[dict]  # each: {attributes: {...}, rings: [[(lon,lat),...]]}
    cache_key: str
    query_version: int
    fetched_at: str
    from_cache: bool


class StructuresClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        assert settings.osm_cache_dir is not None
        self._cache = DiskCache(
            settings.osm_cache_dir, settings.structures_cache_ttl_s, suffix=".struct.json"
        )
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=settings.http_concurrency,
            max_retries=settings.http_max_retries,
        )

    def cache_key(self, bbox: Bbox) -> str:
        b = bbox.normalized()
        return (
            f"structures/v{QUERY_VERSION}/"
            f"{b.min_lon:.6f},{b.min_lat:.6f},{b.max_lon:.6f},{b.max_lat:.6f}"
        )

    async def fetch(self, bbox: Bbox) -> StructuresResult:
        key = self.cache_key(bbox)
        cached = self._cache.get(key)
        if cached is not None:
            log.info("structures cache hit %s", key)
            return StructuresResult(
                features=json.loads(cached),
                cache_key=key,
                query_version=QUERY_VERSION,
                fetched_at=_iso(self._cache.mtime(key) or 0.0),
                from_cache=True,
            )
        features = await self._query_all(bbox)
        self._cache.put(key, json.dumps(features).encode("utf-8"))
        log.info("structures fetch %s -> %d buildings", key, len(features))
        return StructuresResult(
            features=features,
            cache_key=key,
            query_version=QUERY_VERSION,
            fetched_at=_iso(self._cache.mtime(key) or 0.0),
            from_cache=False,
        )

    async def _query_all(self, bbox: Bbox) -> list[dict]:
        b = bbox.normalized()
        out: list[dict] = []
        offset = 0
        while True:
            params = {
                "geometry": f"{b.min_lon},{b.min_lat},{b.max_lon},{b.max_lat}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": _OUT_FIELDS,
                "returnGeometry": "true",
                "geometryPrecision": "6",
                "resultOffset": str(offset),
                "f": "json",
            }
            try:
                resp = await self._http.get(
                    self._s.structures_url, params=params, timeout=60.0
                )
            except httpx.HTTPError as exc:
                raise StructuresUnreachable(str(exc)) from exc
            if resp.status_code != 200:
                raise StructuresBadResponse(f"structures returned {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise StructuresBadResponse(f"unparseable structures response: {exc}") from exc
            if "error" in data:
                raise StructuresBadResponse(f"structures error: {data['error']}")

            batch = data.get("features", [])
            for f in batch:
                rings = (f.get("geometry") or {}).get("rings") or []
                out.append({"attributes": f.get("attributes", {}), "rings": rings})
            if not batch or not data.get("exceededTransferLimit"):
                break
            offset += len(batch)
            if offset > 50000:
                log.warning("structures paging hit safety cap at %d", offset)
                break
        return out

    async def aclose(self) -> None:
        await self._http.aclose()


def _iso(unix_ts: float) -> str:
    return (
        _dt.datetime.fromtimestamp(unix_ts, tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
