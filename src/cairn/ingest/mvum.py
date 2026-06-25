"""Ingest USFS MVUM roads as drivable ways (UoW-4).

Each MVUM road becomes a `drivable` osm_features row with synthesized OSM-like
tags so the existing road_character / rough / terminus logic works unchanged:
  - highway=track, operator="US Forest Service" (forest-road score floor)
  - ref/name from the MVUM id/name (e.g. "2196A" / "DIXON SPUR A")
  - surface from the MVUM surface type
  - smoothness=very_bad when the road is "not maintained for passenger car"
    (drives the informational rough flag — not a score penalty)
  - mvum_access kept for a future legality overlay (not scored, TLC-3)

Keyed by a STABLE hash of the road geometry — NOT the ArcGIS objectid. The MVUM
FeatureServer hands back a fresh objectid for the same physical road on each
query, so an objectid-based key let duplicate copies of one road pile into the
`drivable` table on every re-score: those duplicates masked the road's own
dead-ends in find_termini (a coincident copy looks like "another road connects
here") and inflated remoteness. A geometry hash makes re-ingest a true no-op
(TLC-11) and collapses the service's duplicate records onto one row. Ids land in
a band disjoint from OSM way ids (the road_character lookup is keyed by id).
"""

from __future__ import annotations

import hashlib
import json
import logging

from cairn.store import Store

log = logging.getLogger("cairn.ingest.mvum")

# MVUM ids live in [OFFSET, OFFSET + SPAN), disjoint from OSM way ids (< ~1.5e9)
# and from MN-SFR (2e15+). The id is a hash of the geometry, not the objectid.
MVUM_ID_OFFSET = 1_000_000_000_000_000
MVUM_ID_SPAN = 1_000_000_000_000_000


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


# Coordinates are keyed at the service's geometryPrecision (6 dp). Rounding to a
# fixed precision (rather than hashing the raw WKT text) is what lets the
# one-time dedupe migration reproduce a row's id from its STORED geometry and so
# re-key existing rows to match future ingests exactly.
_GEOM_KEY_DP = 6


def mvum_geom_key(paths: list) -> str | None:
    """Canonical, precision-stable key for an MVUM road's geometry: each path's
    (lon,lat) rounded to 6 dp and joined, paths joined in order. <2-point paths
    are dropped (they carry no line). None when nothing usable remains. The
    dedupe migration builds the identical key from stored geometry."""
    segs = []
    for path in paths:
        pts = [(round(p[0], _GEOM_KEY_DP), round(p[1], _GEOM_KEY_DP)) for p in path if len(p) >= 2]
        if len(pts) >= 2:
            segs.append(",".join(f"{x}:{y}" for x, y in pts))
    return "|".join(segs) if segs else None


def stable_mvum_id(paths: list) -> int | None:
    """Deterministic `drivable` id for an MVUM road from its geometry. Identical
    geometry -> identical id, so the service's duplicate records (same road,
    different objectid) collapse to one row. Disjoint from OSM way ids. None when
    the geometry is unusable."""
    key = mvum_geom_key(paths)
    if key is None:
        return None
    h = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return MVUM_ID_OFFSET + int.from_bytes(h, "big") % MVUM_ID_SPAN


def mvum_rows(features: list[dict]) -> list[tuple]:
    """Pure: MVUM service features -> upsert rows
    (osm_type, osm_id, feature_class, tags_json, wkt).

    Keyed by `stable_mvum_id(geometry)` so duplicate service records collapse to
    one row (dedup within the batch too). Features without usable geometry are
    skipped."""
    rows: list[tuple] = []
    seen: set[int] = set()
    for feat in features:
        paths = feat.get("paths", [])
        oid = stable_mvum_id(paths)
        wkt = paths_to_wkt(paths)
        if oid is None or wkt is None:
            continue
        if oid in seen:  # same geometry twice in this batch -> ingest once
            continue
        seen.add(oid)
        tags = mvum_tags(feat.get("attributes", {}))
        rows.append(
            ("mvum", oid, "drivable", json.dumps(tags, separators=(",", ":")), wkt)
        )
    return rows


async def ingest_mvum(store: Store, features: list[dict], *, chunk_size: int) -> int:
    """Upsert MVUM roads as drivable ways. Returns the count ingested (distinct
    geometries — duplicate service records are collapsed)."""
    rows = mvum_rows(features)
    for i in range(0, len(rows), chunk_size):
        await store.upsert_osm_chunk(rows[i : i + chunk_size])
    skipped = len(features) - len(rows)
    if skipped:
        log.info("mvum ingest skipped/deduped %d features", skipped)
    log.info("mvum ingest: %d roads", len(rows))
    return len(rows)
