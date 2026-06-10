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


def _ring_wkt(points: list[tuple[float, float]]) -> str:
    return "(" + ", ".join(f"{x} {y}" for x, y in points) + ")"


def element_to_wkt(element: dict, feature_class: str) -> str | None:
    """Build a WKT geometry for an OSM element, or None if not representable.

    - node            -> POINT
    - way (area cls)  -> POLYGON when closed, else LINESTRING
    - way (line cls)  -> LINESTRING
    - relation        -> MULTIPOLYGON from member ways (best-effort, outer
                         rings only; inner holes ignored in v1)
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
        rings: list[str] = []
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
                rings.append(f"({_ring_wkt(pts)})")
        if rings:
            return "MULTIPOLYGON(" + ", ".join(rings) + ")"
        return None

    return None


async def ingest_osm(
    store: Store, elements: list[dict], *, chunk_size: int
) -> dict[str, int]:
    """Classify + geometrize + chunk-insert OSM elements. Returns class counts."""
    counts: dict[str, int] = {}
    buffer: list[tuple] = []
    skipped = 0

    async def flush() -> None:
        if buffer:
            await store.upsert_osm_chunk(buffer)
            buffer.clear()

    for element in elements:
        feature_class = classify_osm(element)
        if feature_class is None:
            continue
        wkt = element_to_wkt(element, feature_class)
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
