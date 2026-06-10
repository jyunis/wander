"""USFS Motor Vehicle Use Map (MVUM) road client.

National-forest roads — including the numbered spurs (e.g. "2196A / Dixon Spur
A") that OpenStreetMap frequently doesn't have. Public ArcGIS REST service, no
key (TLC-12: read-only GET). Cached, with backoff, like the other clients.

Returns road features with geometry (lon/lat polylines) + the attributes the
ingest layer needs to synthesize OSM-like tags (id/ref, name, surface, the
"open to vehicles" class, and a passenger-car-suitability class that drives the
rough flag). Non-fatal: the run degrades to OSM-only roads if MVUM is down.
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
from cairn.errors import MvumBadResponse, MvumUnreachable
from cairn.store.types import Bbox

log = logging.getLogger("cairn.mvum")

# Bump if the query (fields/layer) changes — the cache key includes it.
QUERY_VERSION = 1
_OUT_FIELDS = "objectid,id,name,surfacetype,mvum_symbol_name,sbs_symbol_name"


@dataclass
class MvumResult:
    features: list[dict]  # each: {attributes: {...}, paths: [[(lon,lat),...]]}
    cache_key: str
    query_version: int
    fetched_at: str
    from_cache: bool


class MvumClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        assert settings.osm_cache_dir is not None
        self._cache = DiskCache(
            settings.osm_cache_dir, settings.mvum_cache_ttl_s, suffix=".mvum.json"
        )
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=settings.http_concurrency,
            max_retries=settings.http_max_retries,
        )

    def cache_key(self, bbox: Bbox) -> str:
        b = bbox.normalized()
        return (
            f"mvum/v{QUERY_VERSION}/"
            f"{b.min_lon:.6f},{b.min_lat:.6f},{b.max_lon:.6f},{b.max_lat:.6f}"
        )

    async def fetch(self, bbox: Bbox) -> MvumResult:
        key = self.cache_key(bbox)
        cached = self._cache.get(key)
        if cached is not None:
            log.info("mvum cache hit %s", key)
            return MvumResult(
                features=json.loads(cached),
                cache_key=key,
                query_version=QUERY_VERSION,
                fetched_at=_iso(self._cache.mtime(key) or 0.0),
                from_cache=True,
            )

        features = await self._query_all(bbox)
        self._cache.put(key, json.dumps(features).encode("utf-8"))
        log.info("mvum fetch %s -> %d roads", key, len(features))
        return MvumResult(
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
                resp = await self._http.get(self._s.mvum_url, params=params, timeout=60.0)
            except httpx.HTTPError as exc:
                raise MvumUnreachable(str(exc)) from exc
            if resp.status_code != 200:
                raise MvumBadResponse(f"mvum returned {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise MvumBadResponse(f"unparseable mvum response: {exc}") from exc
            if "error" in data:
                raise MvumBadResponse(f"mvum error: {data['error']}")

            batch = data.get("features", [])
            for f in batch:
                paths = (f.get("geometry") or {}).get("paths") or []
                out.append({"attributes": f.get("attributes", {}), "paths": paths})
            if not batch or not data.get("exceededTransferLimit"):
                break
            offset += len(batch)
            if offset > 20000:  # safety: never page unbounded
                log.warning("mvum paging hit safety cap at %d", offset)
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
