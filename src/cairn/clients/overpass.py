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
TRAILS_QUERY_VERSION = 1  # display-only hiking-trails overlay (separate cache key)

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

# Foot/non-motorized ways for the display-only Trails overlay (never scored —
# TLC-3). `track` is intentionally excluded: it's a drivable road (the Roads
# overlay), not a hiking trail.
_FOOT_PATHS = ("path", "footway", "bridleway", "cycleway", "steps")


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


def build_trails_query(bbox: Bbox, timeout_s: int = 60) -> str:
    """Overpass QL for the display-only Trails overlay: foot/non-motorized paths
    plus named hiking ROUTE relations (so a through-trail like the North Country
    Trail keeps its name even where the underlying way is unnamed). `out geom`
    returns way + relation-member geometry inline. Never scored (TLC-3)."""
    b = bbox.normalized()
    box = f"{b.min_lat},{b.min_lon},{b.max_lat},{b.max_lon}"
    paths = _re(_FOOT_PATHS)
    # `out geom({box})` CLIPS returned geometry to the bbox — essential for the
    # route relations: a through-trail like the North Country Trail spans the
    # whole state, and unclipped `out geom` would return its entire statewide
    # member geometry per viewport (thousands of off-screen segments).
    return (
        f"[out:json][timeout:{timeout_s}];"
        "("
        f'way["highway"~"{paths}"]({box});'
        f'relation["route"="hiking"]["name"]({box});'
        ");"
        f"out geom({box});"
    )


def _segments(geometry) -> list[list[list[float]]]:
    """Overpass `geometry` ([{lat,lon}|null,...]) -> GeoJSON [[lon,lat],...]
    segments. `out geom(bbox)` clips to the bbox and marks out-of-bbox vertices
    as `null`; split on those so a clipped way becomes one or more line pieces
    (never a straight line drawn across the gap), each with >= 2 points."""
    segs: list[list[list[float]]] = []
    cur: list[list[float]] = []
    for g in geometry or []:
        if g and "lon" in g and "lat" in g:
            cur.append([g["lon"], g["lat"]])
        else:  # null / clipped-out vertex -> break the run
            if len(cur) >= 2:
                segs.append(cur)
            cur = []
    if len(cur) >= 2:
        segs.append(cur)
    return segs


def trails_to_geojson(elements: list[dict], disclaimer: str) -> dict:
    """Overpass trail elements -> a GeoJSON LineString FeatureCollection.

    Named hiking-route relations win: their member ways are emitted labelled
    with the route name and those way ids are then suppressed as standalone
    features (so the North Country Trail draws once, named, not twice)."""
    features: list[dict] = []
    routed: set[int] = set()

    for el in elements:
        if el.get("type") != "relation" or el.get("tags", {}).get("route") != "hiking":
            continue
        name = el.get("tags", {}).get("name")
        for m in el.get("members", []):
            if m.get("type") != "way":
                continue
            routed.add(m.get("ref"))
            for seg in _segments(m.get("geometry")):
                features.append(_trail_feature(seg, name, "hiking_route"))

    for el in elements:
        if el.get("type") != "way" or el.get("id") in routed:
            continue
        tags = el.get("tags", {})
        for seg in _segments(el.get("geometry")):
            features.append(_trail_feature(seg, tags.get("name"), tags.get("highway"), tags))

    return {
        "type": "FeatureCollection",
        "disclaimer": disclaimer,
        "features": features,
    }


def _trail_feature(coords: list[list[float]], name, kind, tags: dict | None = None) -> dict:
    tags = tags or {}
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "name": name or None,
            "kind": kind,
            "sac_scale": tags.get("sac_scale"),
            "surface": tags.get("surface"),
        },
    }


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

    async def fetch_trails(self, bbox: Bbox) -> OverpassResult:
        """Foot/non-motorized trails + named hiking routes for the display-only
        Trails overlay. Separate cache key from the scoring query."""
        return await self._fetch(
            self._key("overpass-trails", TRAILS_QUERY_VERSION, bbox),
            build_trails_query(bbox, timeout_s=int(self._s.overpass_timeout_s)),
            TRAILS_QUERY_VERSION,
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
