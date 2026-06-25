"""Ingest hiking trails (OSM) as a display-only `trail` overlay (TLC-3).

Foot/non-motorized paths + named hiking ROUTE relations (e.g. the North Country
Trail). Bulk-preloaded by scripts/load_trails.py into `osm_features`
(feature_class='trail') so the /trails overlay reads LOCAL data — public Overpass
is far too flaky to depend on a live per-viewport query. Never scored (TLC-3).

Stored as whole (unclipped) LineStrings keyed (osm_type='trail', osm_id=way id);
the /trails read clips to the viewport (ST_Intersection), exactly like roads.
"""

from __future__ import annotations

import json


def linestring_wkt(geometry) -> str | None:
    """Overpass `geometry` ([{lat,lon},...]) -> LINESTRING WKT, or None if < 2 pts."""
    pts = [(g["lon"], g["lat"]) for g in (geometry or []) if g and "lon" in g and "lat" in g]
    if len(pts) < 2:
        return None
    return "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in pts) + ")"


def trail_rows(elements: list[dict]) -> list[tuple]:
    """Overpass trail elements -> osm_features upsert rows
    (osm_type, osm_id, feature_class, tags_json, wkt).

    Named hiking-route relations win: their member ways are stored labelled with
    the route name and deduped against standalone ways, so a through-trail like
    the NCT is stored once, named (not twice)."""
    tags_by_way: dict[int, dict] = {}
    wkt_by_way: dict[int, str] = {}

    for el in elements:
        if el.get("type") != "relation" or el.get("tags", {}).get("route") != "hiking":
            continue
        name = el.get("tags", {}).get("name")
        for m in el.get("members", []):
            if m.get("type") != "way":
                continue
            wid = m.get("ref")
            wkt = linestring_wkt(m.get("geometry"))
            if wid is None or wkt is None:
                continue
            tags_by_way[wid] = {"name": name, "kind": "hiking_route"}
            wkt_by_way[wid] = wkt

    for el in elements:
        if el.get("type") != "way":
            continue
        wid = el.get("id")
        if wid is None or wid in tags_by_way:
            continue
        wkt = linestring_wkt(el.get("geometry"))
        if wkt is None:
            continue
        t = el.get("tags", {})
        tags = {"name": t.get("name") or None, "kind": t.get("highway")}
        if t.get("sac_scale"):
            tags["sac_scale"] = t["sac_scale"]
        if t.get("surface"):
            tags["surface"] = t["surface"]
        tags_by_way[wid] = tags
        wkt_by_way[wid] = wkt

    return [
        ("trail", wid, "trail", json.dumps(tags, separators=(",", ":")), wkt_by_way[wid])
        for wid, tags in tags_by_way.items()
    ]
