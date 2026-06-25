"""Ingest MN DNR State Forest Roads as drivable ways (doc #34 follow-up).

MN *state* forest roads — the DNR-administered access roads inside state forests
(e.g. Pillsbury). These fill the gap left by USFS MVUM (national-forest only) and
by OpenStreetMap, which often lacks the two-tracks. Source: MN Geospatial Commons
"MNDNR Administered State Forest Roads" ArcGIS FeatureServer (~1,632 statewide
segments); bulk-loaded by scripts/load_mn_sfr.py.

Each segment becomes a `drivable` osm_features row with synthesized OSM-like tags
so the existing road_character / rough / terminus logic works unchanged:
  - highway=track, operator="MN DNR" (forest-road score floor)
  - ref/name from road_num / road_name
  - smoothness=very_bad for a "Minimum Maintenance Road" (drives the informational
    rough flag — not a score penalty); a "System Forest Road" stays un-flagged
  - mn_sfr_class kept for reference (NOT scored, TLC-3)

Keyed (osm_type='mnsfr', osm_id=objectid+OFFSET) so it's idempotent (TLC-11) and
its id range is disjoint from OSM way ids and the USFS MVUM range.
"""

from __future__ import annotations

import json
import logging

from cairn.ingest.mvum import paths_to_wkt  # generic ArcGIS paths -> WKT
from cairn.store import Store

log = logging.getLogger("cairn.ingest.mn_sfr")

# Disjoint from OSM way ids (< ~1.5e9) and the MVUM range (1e15 + objectid).
MN_SFR_ID_OFFSET = 2_000_000_000_000_000


def mn_sfr_tags(attr: dict) -> dict:
    """Synthesize OSM-like tags from MN state-forest-road attributes."""
    road_class = (attr.get("road_class") or "").strip()
    tags = {
        "highway": "track",
        "operator": "MN DNR",
        "source": "mn_sfr",
        "ref": str(attr.get("road_num") or "").strip(),
        "name": str(attr.get("road_name") or "").strip(),
    }
    if road_class:
        tags["mn_sfr_class"] = road_class
    # Minimum-maintenance roads aren't kept up for passenger cars -> rough flag
    # (informational, mirroring MVUM's "not maintained for passenger car").
    if "minimum maintenance" in road_class.lower():
        tags["smoothness"] = "very_bad"
    return tags


async def ingest_mn_sfr(store: Store, features: list[dict], *, chunk_size: int) -> int:
    """Upsert MN state forest roads as drivable ways. Returns the count ingested.

    `features`: each ``{attributes: {...}, paths: [[(lon, lat), ...]]}`` — the
    ArcGIS shape, queried with ``outSR=4326`` so paths are already lon/lat."""
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
        buffer.append(
            (
                "mnsfr",
                int(objectid) + MN_SFR_ID_OFFSET,
                "drivable",
                json.dumps(mn_sfr_tags(attr), separators=(",", ":")),
                wkt,
            )
        )
        count += 1
        if len(buffer) >= chunk_size:
            await flush()
    await flush()

    if skipped:
        log.info("mn_sfr ingest skipped %d features (no geometry/id)", skipped)
    log.info("mn_sfr ingest: %d roads", count)
    return count
