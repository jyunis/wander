"""Ingest USFS MVUM roads as drivable ways (UoW-4).

Each MVUM road becomes a `drivable` osm_features row with synthesized OSM-like
tags so the existing road_character / rough / terminus logic works unchanged:
  - highway=track, operator="US Forest Service" (forest-road score floor)
  - ref/name from the MVUM id/name (e.g. "2196A" / "DIXON SPUR A")
  - surface from the MVUM surface type
  - smoothness=very_bad when the road is "not maintained for passenger car"
    (drives the informational rough flag — not a score penalty)
  - mvum_access kept for a future legality overlay (not scored, TLC-3)

Keyed (osm_type='mvum', osm_id=objectid+OFFSET) so it's idempotent (TLC-11)
and never collides with OSM way ids in the shared `drivable` id space.
"""

from __future__ import annotations

import json
import logging

from cairn.store import Store

log = logging.getLogger("cairn.ingest.mvum")

# Keep MVUM ids in a disjoint numeric range from OSM way ids (the drivable
# road_character lookup is keyed by id alone).
MVUM_ID_OFFSET = 1_000_000_000_000_000


def mvum_tags(attr: dict) -> dict:
    """Synthesize OSM-like tags from MVUM attributes."""
    surface_raw = (attr.get("surfacetype") or "").upper()
    if "AGG" in surface_raw or "GRAVEL" in surface_raw:
        surface = "gravel"
    elif "NAT" in surface_raw or "NATIVE" in surface_raw:
        surface = "ground"
    elif "IMP" in surface_raw or "PAVED" in surface_raw or "ASPH" in surface_raw:
        surface = "paved"
    else:
        surface = None

    tags = {
        "highway": "track",
        "operator": "US Forest Service",
        "source": "usfs_mvum",
        "ref": str(attr.get("id") or "").strip(),
        "name": str(attr.get("name") or "").strip(),
    }
    if surface:
        tags["surface"] = surface
    # Passenger-car suitability -> rough flag (informational).
    sbs = (attr.get("sbs_symbol_name") or "").lower()
    if "not maintained for passenger" in sbs:
        tags["smoothness"] = "very_bad"
    # Vehicle-access class — kept for a future legality overlay, NOT scored.
    access_class = attr.get("mvum_symbol_name")
    if access_class:
        tags["mvum_access"] = access_class
    return tags


def paths_to_wkt(paths: list) -> str | None:
    """ArcGIS polyline paths (lon/lat) -> LINESTRING / MULTILINESTRING WKT."""
    rings = []
    for path in paths:
        pts = [(p[0], p[1]) for p in path if len(p) >= 2]
        if len(pts) >= 2:
            rings.append("(" + ", ".join(f"{x} {y}" for x, y in pts) + ")")
    if not rings:
        return None
    if len(rings) == 1:
        return f"LINESTRING{rings[0]}"
    return "MULTILINESTRING(" + ", ".join(rings) + ")"


async def ingest_mvum(store: Store, features: list[dict], *, chunk_size: int) -> int:
    """Upsert MVUM roads as drivable ways. Returns the count ingested."""
    buffer: list[tuple] = []
    count = 0
    skipped = 0

    async def flush() -> None:
        if buffer:
            await store.upsert_osm_chunk(buffer)
            buffer.clear()

    for feat in features:
        attr = feat.get("attributes", {})
        wkt = paths_to_wkt(feat.get("paths", []))
        objectid = attr.get("objectid")
        if wkt is None or objectid is None:
            skipped += 1
            continue
        tags = mvum_tags(attr)
        buffer.append(
            (
                "mvum",
                int(objectid) + MVUM_ID_OFFSET,
                "drivable",
                json.dumps(tags, separators=(",", ":")),
                wkt,
            )
        )
        count += 1
        if len(buffer) >= chunk_size:
            await flush()
    await flush()

    if skipped:
        log.info("mvum ingest skipped %d features (no geometry/id)", skipped)
    log.info("mvum ingest: %d roads", count)
    return count
