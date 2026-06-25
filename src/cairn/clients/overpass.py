"""Overpass (OpenStreetMap) client (FC-1, TLC-14).

Builds one versioned, tightly tag-filtered query per bbox — only the feature
classes Cairn scores, nothing else — to bound the response size (TLC-14).
GET-only with a descriptive User-Agent (TLC-12/5), cache-first keyed by
(normalized_bbox, query_version) with a TTL, bounded backoff on 429/5xx.
Returns parsed elements + the cache key/timestamp for provenance (TLC-2);
classification and chunked ingest happen downstream.

Bump `QUERY_VERSION` on ANY change to the query template — the cache key
includes it, so old cached extracts are never silently reused under new query
semantics.
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
from cairn.errors import OverpassBadResponse, OverpassTimeout, OverpassUnreachable
from cairn.store.types import Bbox

log = logging.getLogger("cairn.overpass")

QUERY_VERSION = 2  # +building (dispersed sites must avoid houses/cabins)

# --- Scored tag classes (the ONLY things the query asks for — TLC-14) ------
_DRIVABLE = ("track", "unclassified", "service", "residential", "tertiary")
_MAJOR_HIGHWAY = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "motorway_link",
    "trunk_link",
    "primary_link",
)
_WATERWAYS = ("river", "stream", "canal")
_BUILTUP = ("residential", "commercial", "industrial", "retail")
_PLACES = ("city", "town", "village", "hamlet")


def _re(values: tuple[str, ...]) -> str:
    return "^(" + "|".join(values) + ")$"


def build_query(bbox: Bbox, timeout_s: int = 180) -> str:
    """Assemble the versioned, tag-filtered Overpass QL for a bbox.

    bbox is emitted in Overpass order: (south, west, north, east).
    """
    b = bbox.normalized()
    box = f"{b.min_lat},{b.min_lon},{b.max_lat},{b.max_lon}"
    drivable = _re(_DRIVABLE)
    major = _re(_MAJOR_HIGHWAY)
    waterways = _re(_WATERWAYS)
    builtup = _re(_BUILTUP)
    places = _re(_PLACES)
    return (
        f"[out:json][timeout:{timeout_s}];"
        "("
        f'way["highway"~"{drivable}"]({box});'
        f'way["highway"~"{major}"]({box});'
        f'way["natural"="water"]({box});'
        f'relation["natural"="water"]({box});'
        f'way["waterway"~"{waterways}"]({box});'
        f'way["landuse"="forest"]({box});'
        f'relation["landuse"="forest"]({box});'
        f'way["natural"="wood"]({box});'
        f'relation["natural"="wood"]({box});'
        f'way["landuse"~"{builtup}"]({box});'
        f'node["place"~"{places}"]({box});'
        f'node["tourism"="camp_site"]({box});'
        f'way["tourism"="camp_site"]({box});'
        f'way["building"]({box});'
        f'relation["building"]({box});'
        ");"
        "out geom;"
    )


def classify_osm(element: dict) -> str | None:
    """Map a returned OSM element to its scored feature class, or None.

    Disambiguates by tag KEY (e.g. highway=residential is drivable; while
    landuse=residential is built-up). Order encodes precedence.
    """
    tags = element.get("tags", {})
    hw = tags.get("highway")
    if hw in _DRIVABLE:
        return "drivable"
    if hw in _MAJOR_HIGHWAY:
        return "highway"
    if tags.get("tourism") == "camp_site":
        return "campsite"
    if tags.get("natural") == "water" or tags.get("waterway") in _WATERWAYS:
        return "water"
    if tags.get("landuse") == "forest" or tags.get("natural") == "wood":
        return "forest"
    if tags.get("landuse") in _BUILTUP:
        return "builtup"
    if tags.get("place") in _PLACES:
        return "place"
    if tags.get("building"):
        return "building"
    return None


@dataclass
class OverpassResult:
    """A fetched (or cache-served) OSM extract + its provenance handles."""

    elements: list[dict]
    cache_key: str
    query_version: int
    fetched_at: str  # ISO8601 UTC — when the underlying data was fetched
    from_cache: bool


class OverpassClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        assert settings.osm_cache_dir is not None
        self._cache = DiskCache(
            settings.osm_cache_dir, settings.osm_cache_ttl_s, suffix=".json"
        )
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=settings.http_concurrency,
            max_retries=settings.http_max_retries,
        )

    def cache_key(self, bbox: Bbox) -> str:
        return self._key("overpass", QUERY_VERSION, bbox)

    @staticmethod
    def _key(prefix: str, version: int, bbox: Bbox) -> str:
        b = bbox.normalized()
        return (
            f"{prefix}/v{version}/"
            f"{b.min_lon:.6f},{b.min_lat:.6f},{b.max_lon:.6f},{b.max_lat:.6f}"
        )

    async def fetch(self, bbox: Bbox) -> OverpassResult:
        return await self._fetch(
            self.cache_key(bbox),
            build_query(bbox, timeout_s=int(self._s.overpass_timeout_s)),
            QUERY_VERSION,
        )

    async def _fetch(self, key: str, query: str, query_version: int) -> OverpassResult:
        cached = self._cache.get(key)
        if cached is not None:
            mtime = self._cache.mtime(key) or 0.0
            log.info("overpass cache hit %s", key)
            return OverpassResult(
                elements=self._parse(cached).get("elements", []),
                cache_key=key,
                query_version=query_version,
                fetched_at=_iso(mtime),
                from_cache=True,
            )

        log.info("overpass fetch %s", key)
        try:
            resp = await self._http.get(
                self._s.overpass_url,
                params={"data": query},
                timeout=self._s.overpass_timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise OverpassTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise OverpassUnreachable(str(exc)) from exc

        if resp.status_code != 200:
            raise OverpassBadResponse(
                f"overpass returned {resp.status_code}", status_code=resp.status_code
            )
        body = resp.content
        parsed = self._parse(body)
        self._cache.put(key, body)
        return OverpassResult(
            elements=parsed.get("elements", []),
            cache_key=key,
            query_version=query_version,
            fetched_at=_iso(self._cache.mtime(key) or 0.0),
            from_cache=False,
        )

    def _parse(self, body: bytes) -> dict:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OverpassBadResponse(f"unparseable overpass response: {exc}") from exc

    async def aclose(self) -> None:
        await self._http.aclose()


def _iso(unix_ts: float) -> str:
    return (
        _dt.datetime.fromtimestamp(unix_ts, tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
