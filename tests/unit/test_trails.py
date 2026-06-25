"""Trails overlay: Overpass query + GeoJSON conversion (display-only, TLC-3).

Foot/non-motorized paths plus named hiking ROUTES (e.g. the North Country
Trail). Route relations win: their member ways draw once, labelled with the
route name; `track` is never a trail (that's the Roads overlay)."""

from __future__ import annotations

from cairn.clients.overpass import build_trails_query, trails_to_geojson
from cairn.store.types import Bbox

DISC = "physical suitability only"


def test_build_trails_query_targets_paths_and_hiking_routes():
    q = build_trails_query(Bbox(-94.5, 46.9, -94.2, 47.1))
    assert 'way["highway"~"^(path|footway|bridleway|cycleway|steps)$"]' in q
    assert 'relation["route"="hiking"]["name"]' in q
    assert "out geom(46.9,-94.5,47.1,-94.2);" in q  # geometry clipped to the bbox
    assert '"track"' not in q  # track is a drivable road, not a trail


def _way(wid, coords, **tags):
    return {
        "type": "way", "id": wid, "tags": tags,
        "geometry": [{"lat": la, "lon": lo} for lo, la in coords],
    }


def test_named_route_wins_and_dedupes_member_ways():
    # The NCT relation has member way 100 (also returned standalone) + way 101.
    nct_geom = [(-94.40, 47.00), (-94.38, 47.00)]
    elements = [
        {"type": "relation", "id": 9, "tags": {"route": "hiking", "name": "North Country Trail"},
         "members": [
             {"type": "way", "ref": 100, "geometry": [{"lat": 47.00, "lon": -94.40},
                                                       {"lat": 47.00, "lon": -94.38}]},
             {"type": "way", "ref": 101, "geometry": [{"lat": 47.00, "lon": -94.38},
                                                       {"lat": 47.00, "lon": -94.36}]},
         ]},
        _way(100, nct_geom, highway="path"),                      # suppressed (in the route)
        _way(200, [(-94.30, 47.05), (-94.29, 47.06)], highway="footway", name="Lakeshore Path"),
    ]
    gj = trails_to_geojson(elements, DISC)
    feats = gj["features"]
    assert gj["type"] == "FeatureCollection" and gj["disclaimer"] == DISC
    # 2 route members (named) + 1 standalone footway; the standalone way 100 is
    # suppressed because it's part of the route.
    routes = [f for f in feats if f["properties"]["kind"] == "hiking_route"]
    paths = [f for f in feats if f["properties"]["kind"] == "footway"]
    assert len(routes) == 2
    assert all(f["properties"]["name"] == "North Country Trail" for f in routes)
    assert len(paths) == 1 and paths[0]["properties"]["name"] == "Lakeshore Path"
    # geometry is GeoJSON [lon, lat] LineStrings
    assert routes[0]["geometry"]["type"] == "LineString"
    assert routes[0]["geometry"]["coordinates"][0] == [-94.40, 47.00]


def test_degenerate_geometry_dropped():
    elements = [_way(1, [(-94.4, 47.0)], highway="path")]  # single point -> no line
    assert trails_to_geojson(elements, DISC)["features"] == []
