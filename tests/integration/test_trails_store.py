"""Trails read path: replace_trails + get_trails_geojson clip to the viewport."""

from __future__ import annotations

from cairn.ingest.trails import trail_rows
from cairn.store.types import Bbox

ELEMENTS = [
    {"type": "relation", "id": 9,
     "tags": {"route": "hiking", "name": "North Country Trail"},
     "members": [{"type": "way", "ref": 100,
                  "geometry": [{"lat": 47.0, "lon": -94.40}, {"lat": 47.0, "lon": -94.30}]}]},
    {"type": "way", "id": 200, "tags": {"highway": "path", "name": "Lakeshore Path"},
     "geometry": [{"lat": 47.5, "lon": -93.0}, {"lat": 47.5, "lon": -92.9}]},  # far away
]


async def test_replace_and_read_clips_to_bbox(store):
    n = await store.replace_trails(trail_rows(ELEMENTS))
    assert n == 2

    # A bbox over the NCT only — the far-away Lakeshore Path is excluded.
    gj = await store.get_trails_geojson(Bbox(-94.45, 46.95, -94.25, 47.05))
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    names = {f["properties"]["name"] for f in gj["features"]}
    assert "North Country Trail" in names and "Lakeshore Path" not in names
    assert all(f["geometry"]["type"] == "LineString" for f in gj["features"])

    # replace_trails is wholesale.
    await store.replace_trails([])
    empty = await store.get_trails_geojson(Bbox(-95, 46, -92, 48))
    assert empty["features"] == []
