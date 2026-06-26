"""Streamed, chunked OSM ingest (UoW-4, TLC-14).

Iterates the Overpass elements one at a time, classifies each to a scored
feature class, builds its geometry as WKT, and flushes to SQLite in bounded
chunks with a per-chunk commit. Never holds the whole parsed blob *plus* all
derived geometries simultaneously — at most one chunk of geometries is live.
Ingest is idempotent (keyed upsert, TLC-11).
"""

from __future__ import annotations

import json
import logging

from cairn.clients.overpass import classify_osm
from cairn.store import Store

log = logging.getLogger("cairn.ingest")

# Classes whose closed ways/relations are areas (polygon semantics: a point
# inside has distance 0). Linear/point classes stay lines/points.
_AREA_CLASSES = {"water", "forest", "builtup", "campsite", "building"}

# Area classes a continent-spanning relation (Lake Superior — one ~219k-vertex
# relation whose MBR covers the whole region, so the R*Tree can't prune it and
# every candidate measures ST_Distance against the full lake) makes pathological.
# When a clip box is supplied at ingest, relation member rings of these classes
# that fall entirely outside it are dropped.
_CLIP_CLASSES = {"water", "forest", "builtup", "building"}


def _ring_wkt(points: list[tuple[float, float]]) -> str:
    return "(" + ", ".join(f"{x} {y}" for x, y in points) + ")"


def _ring_touches(pts: list[tuple[float, float]], box: tuple[float, float, float, float]) -> bool:
    """True if any vertex lies within `box` (min_lon, min_lat, max_lon, max_lat).
    A coarse keep-test: member ways are short shoreline fragments, so vertex
    containment is enough to retain every fragment near the box."""
    w, s, e, n = box
    return any(w <= x <= e and s <= y <= n for x, y in pts)


def element_to_wkt(
    element: dict,
    feature_class: str,
    clip_box: tuple[float, float, float, float] | None = None,
    clip_vertex_threshold: int = 2000,
) -> str | None:
    """Build a WKT geometry for an OSM element, or None if not representable.

    - node            -> POINT
    - way (area cls)  -> POLYGON when closed, else LINESTRING
    - way (line cls)  -> LINESTRING
    - relation        -> MULTIPOLYGON from member ways (best-effort, outer
                         rings only; inner holes ignored in v1)

    When `clip_box` (an already-buffered lon/lat box) is given and a relation of
    a `_CLIP_CLASSES` area class exceeds `clip_vertex_threshold` vertices, member
    rings entirely outside the box are dropped. The buffer is sized >= the
    largest distance-feature search radius, so a kept ring covers every shoreline
    within reach of any in-box candidate — the nearest-distance result is
    unchanged; only far-shore vertices nothing measures against are removed.
    """
    etype = element.get("type")
    if etype == "node":
        if "lon" not in element or "lat" not in element:
            return None
        return f"POINT({element['lon']} {element['lat']})"

    if etype == "way":
        geom = element.get("geometry") or []
        pts = [(g["lon"], g["lat"]) for g in geom if "lon" in g and "lat" in g]
        if len(pts) < 2:
            return None
        is_closed = len(pts) >= 4 and pts[0] == pts[-1]
        if is_closed and feature_class in _AREA_CLASSES:
            return f"POLYGON({_ring_wkt(pts)})"
        return "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in pts) + ")"

    if etype == "relation":
        rings: list[list[tuple[float, float]]] = []
        for member in element.get("members", []):
            if member.get("type") != "way":
                continue
            geom = member.get("geometry") or []
            pts = [(g["lon"], g["lat"]) for g in geom if "lon" in g and "lat" in g]
            if len(pts) < 3:
                continue
            if pts[0] != pts[-1]:
                pts.append(pts[0])  # close the ring
            if len(pts) >= 4:
                rings.append(pts)
        if not rings:
            return None
        if (
            clip_box is not None
            and feature_class in _CLIP_CLASSES
            and sum(len(r) for r in rings) > clip_vertex_threshold
        ):
            kept = [r for r in rings if _ring_touches(r, clip_box)]
            if kept:  # keep the full geometry if nothing survives (defensive)
                rings = kept
        return "MULTIPOLYGON(" + ", ".join(f"({_ring_wkt(r)})" for r in rings) + ")"

    return None


def _buffered_box(bbox, buffer_m: float) -> tuple[float, float, float, float]:
    """`bbox` grown by `buffer_m` on every side, as (min_lon, min_lat, max_lon,
    max_lat). Longitude is scaled by cos(lat) so the buffer is ~`buffer_m` in
    both axes."""
    import math

    b = bbox.normalized()
    lat_buf = buffer_m / 111320.0
    cos_lat = math.cos(math.radians((b.min_lat + b.max_lat) / 2))
    lon_buf = buffer_m / (111320.0 * max(cos_lat, 1e-6))
    return (b.min_lon - lon_buf, b.min_lat - lat_buf, b.max_lon + lon_buf, b.max_lat + lat_buf)


async def ingest_osm(
    store: Store,
    elements: list[dict],
    *,
    chunk_size: int,
    clip_bbox=None,
    clip_buffer_m: float = 3500.0,
    clip_vertex_threshold: int = 2000,
) -> dict[str, int]:
    """Classify + geometrize + chunk-insert OSM elements. Returns class counts.

    `clip_bbox` (the run bbox) bounds oversized area-relation geometries to
    bbox+buffer at WKT build (see `element_to_wkt`) — a perf measure that never
    alters a computed in-radius distance. Omitting it stores every geometry
    verbatim."""
    counts: dict[str, int] = {}
    buffer: list[tuple] = []
    skipped = 0
    clip_box = _buffered_box(clip_bbox, clip_buffer_m) if clip_bbox is not None else None

    async def flush() -> None:
        if buffer:
            await store.upsert_osm_chunk(buffer)
            buffer.clear()

    for element in elements:
        feature_class = classify_osm(element)
        if feature_class is None:
            continue
        wkt = element_to_wkt(element, feature_class, clip_box, clip_vertex_threshold)
        if wkt is None:
            skipped += 1
            continue
        buffer.append(
            (
                element.get("type"),
                element.get("id"),
                feature_class,
                json.dumps(element.get("tags", {}), separators=(",", ":")),
                wkt,
            )
        )
        counts[feature_class] = counts.get(feature_class, 0) + 1
        if len(buffer) >= chunk_size:
            await flush()
    await flush()

    if skipped:
        log.info("ingest skipped %d elements with no usable geometry", skipped)
    log.info("ingest classes: %s", counts)
    return counts
