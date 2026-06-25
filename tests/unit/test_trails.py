"""Trails ingest: Overpass elements -> osm_features 'trail' rows (TLC-3).

Named hiking-route relations win + dedupe their member ways; named paths are
stored standalone. Stored unclipped (whole LineStrings); the /trails read clips
to the viewport."""

from __future__ import annotations

import json

from cairn.ingest.trails import linestring_wkt, trail_rows


def _way(wid, coords, **tags):
    return {
        "type": "way", "id": wid, "tags": tags,
        "geometry": [{"lat": la, "lon": lo} for lo, la in coords],
    }


def test_named_route_wins_and_dedupes_member_ways():
    elements = [
        {"type": "relation", "id": 9,
         "tags": {"route": "hiking", "name": "North Country Trail"},
         "members": [
             {"type": "way", "ref": 100,
              "geometry": [{"lat": 47.0, "lon": -94.40}, {"lat": 47.0, "lon": -94.38}]},
             {"type": "way", "ref": 101,
              "geometry": [{"lat": 47.0, "lon": -94.38}, {"lat": 47.0, "lon": -94.36}]},
         ]},
        _way(100, [(-94.40, 47.0), (-94.38, 47.0)], highway="path"),  # suppressed (route member)
        _way(200, [(-94.30, 47.05), (-94.29, 47.06)], highway="path", name="Lakeshore Path"),
    ]
    rows = trail_rows(elements)
    # rows: (osm_type, osm_id, feature_class, tags_json, wkt)
    assert all(r[0] == "trail" and r[2] == "trail" for r in rows)
    by_id = {r[1]: json.loads(r[3]) for r in rows}
    assert set(by_id) == {100, 101, 200}  # way 100 stored once (as a route member)
    assert by_id[100]["kind"] == "hiking_route" and by_id[100]["name"] == "North Country Trail"
    assert by_id[101]["name"] == "North Country Trail"
    assert by_id[200]["kind"] == "path" and by_id[200]["name"] == "Lakeshore Path"
    wkt = {r[1]: r[4] for r in rows}
    assert wkt[100].startswith("LINESTRING(") and "-94.4 47" in wkt[100]


def test_degenerate_geometry_dropped():
    assert linestring_wkt([{"lat": 47, "lon": -94}]) is None  # single point
    assert trail_rows([_way(1, [(-94.4, 47.0)], highway="path")]) == []
