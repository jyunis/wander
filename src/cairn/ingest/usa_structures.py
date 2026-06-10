"""Ingest USA Structures as building footprints (UoW-4).

Each footprint becomes a `building` osm_features polygon, so the no-camping-
near-houses setback (FC-4) sees them alongside OSM buildings. Occupancy classes
that aren't worth avoiding for dispersed camping — utility/misc (sheds, vault
toilets, pump houses) and agriculture (barns) — are skipped, so a toilet at a
boat landing doesn't disqualify the site. Keyed (osm_type='usa_struct',
objectid+OFFSET) so it's idempotent and disjoint from OSM/MVUM ids (TLC-11).
"""

from __future__ import annotations

import json
import logging

from cairn.store import Store

log = logging.getLogger("cairn.ingest.structures")

STRUCT_ID_OFFSET = 2_000_000_000_000_000

# Occupancy classes we DON'T avoid (small/amenity structures). Everything else
# (Residential, Commercial, Assembly, Education, Government, Industrial,
# Unclassified, …) is treated as a building to keep clear of.
_SKIP_OCC = {"Utility and Misc", "Agriculture"}


def ring_to_polygon_wkt(rings: list) -> str | None:
    """ArcGIS polygon rings (lon/lat) -> POLYGON / MULTIPOLYGON WKT. Holes are
    treated as separate polygons — harmless for nearest-distance setback."""
    polys = []
    for ring in rings:
        pts = [(p[0], p[1]) for p in ring if len(p) >= 2]
        if len(pts) >= 3:
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            if len(pts) >= 4:
                polys.append("((" + ", ".join(f"{x} {y}" for x, y in pts) + "))")
    if not polys:
        return None
    if len(polys) == 1:
        return f"POLYGON{polys[0]}"  # polys[0] is already "((ring))"
    return "MULTIPOLYGON(" + ", ".join(polys) + ")"


async def ingest_structures(store: Store, features: list[dict], *, chunk_size: int) -> int:
    """Upsert USA Structures footprints as `building` features. Returns count."""
    buffer: list[tuple] = []
    count = 0
    skipped = 0

    async def flush() -> None:
        if buffer:
            await store.upsert_osm_chunk(buffer)
            buffer.clear()

    for feat in features:
        attr = feat.get("attributes", {})
        occ = attr.get("OCC_CLS")
        if occ in _SKIP_OCC:
            skipped += 1
            continue
        wkt = ring_to_polygon_wkt(feat.get("rings", []))
        objectid = attr.get("OBJECTID")
        if wkt is None or objectid is None:
            skipped += 1
            continue
        buffer.append(
            (
                "usa_struct",
                int(objectid) + STRUCT_ID_OFFSET,
                "building",
                json.dumps({"building": "yes", "source": "usa_structures", "occ": occ}),
                wkt,
            )
        )
        count += 1
        if len(buffer) >= chunk_size:
            await flush()
    await flush()

    if skipped:
        log.info("structures ingest skipped %d (utility/ag/no-geom)", skipped)
    log.info("structures ingest: %d buildings", count)
    return count
