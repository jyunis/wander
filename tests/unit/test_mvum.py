"""MVUM ingest helpers: tag synthesis + geometry WKT."""

from __future__ import annotations

from cairn.engine.features import compute_road_score, road_is_rough
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS
from cairn.ingest.mvum import (
    MVUM_ID_OFFSET,
    MVUM_ID_SPAN,
    mvum_geom_key,
    mvum_rows,
    mvum_tags,
    paths_to_wkt,
    stable_mvum_id,
)

ROAD_CFG = DEFAULT_SCORING_PARAMS["features"]["road_character"]

DIXON = {
    "objectid": 8061814,
    "id": "2196A",
    "name": "DIXON SPUR A",
    "surfacetype": "NAT - NATIVE MATERIAL",
    "mvum_symbol_name": "Roads open to all Vehicles, Seasonal",
    "sbs_symbol_name": "Road, Not Maintained for Passenger Car",
}


def test_dixon_spur_tags_score_as_forest_road():
    tags = mvum_tags(DIXON)
    assert tags["highway"] == "track"
    assert tags["operator"] == "US Forest Service"
    assert tags["ref"] == "2196A"
    assert tags["surface"] == "ground"
    # Native-material, not-maintained-for-passenger-car -> rough flag, but a
    # full road_character hit (forest track), not a penalty.
    assert compute_road_score(tags, ROAD_CFG) == 1.0
    assert road_is_rough(tags) is True
    # vehicle-access class retained for a future legality overlay
    assert "Vehicles" in tags["mvum_access"]


def test_gravel_passenger_car_road_is_not_rough():
    tags = mvum_tags(
        {
            "objectid": 1,
            "id": "2535",
            "surfacetype": "AGG - CRUSHED AGGREGATE OR GRAVEL",
            "sbs_symbol_name": "Gravel Road, Suitable for Passenger Car",
        }
    )
    assert tags["surface"] == "gravel"
    assert road_is_rough(tags) is False


def test_paths_to_wkt_line_and_multiline():
    assert paths_to_wkt([[[-94.1, 47.5], [-94.0, 47.6]]]) == (
        "LINESTRING(-94.1 47.5, -94.0 47.6)"
    )
    multi = paths_to_wkt([[[-94.1, 47.5], [-94.0, 47.6]], [[-94.2, 47.4], [-94.3, 47.3]]])
    assert multi.startswith("MULTILINESTRING(")
    assert paths_to_wkt([]) is None
    assert paths_to_wkt([[[-94.1, 47.5]]]) is None  # single point -> nothing


def test_id_offset_keeps_mvum_ids_disjoint_from_osm():
    # OSM way ids are well below the offset; MVUM ids land above it.
    assert MVUM_ID_OFFSET > 10**12


def test_stable_id_is_geometry_keyed():
    a = [[[-94.1, 47.5], [-94.0, 47.6]]]
    b = [[[-94.2, 47.4], [-94.3, 47.3]]]
    # Same geometry -> same id (idempotent); different geometry -> different id.
    assert stable_mvum_id(a) == stable_mvum_id(a)
    assert stable_mvum_id(a) != stable_mvum_id(b)
    # Lands in the disjoint MVUM band, above OSM way ids.
    for paths in (a, b):
        assert MVUM_ID_OFFSET <= stable_mvum_id(paths) < MVUM_ID_OFFSET + MVUM_ID_SPAN
    assert stable_mvum_id([]) is None  # no geometry -> no id


def test_geom_key_is_precision_stable_for_rekey():
    # The migration reproduces a row's id from STORED geometry, which round-trips
    # through SpatiaLite. Keying on coords rounded to 6 dp (not the raw WKT text)
    # makes that reproducible: sub-1e-6 jitter from the round-trip maps to the
    # same key, so re-keying matches what the next ingest computes.
    p1 = [[[-94.1500000, 47.4060000], [-94.1490000, 47.4055000]]]
    p2 = [[[-94.15000004, 47.40599997], [-94.14899996, 47.40550003]]]
    assert mvum_geom_key(p1) == mvum_geom_key(p2)
    assert stable_mvum_id(p1) == stable_mvum_id(p2)


def _feat(objectid, paths, name="FR 3009 SPUR D", ref="3009D"):
    return {
        "attributes": {"objectid": objectid, "id": ref, "name": name},
        "paths": paths,
    }


def test_mvum_rows_collapse_duplicate_geometry_with_different_objectids():
    # The real bug: the service returns the SAME road geometry under three
    # different objectids across re-scores. Keying by geometry collapses them to
    # one row (so a road's dead-end isn't masked by its own duplicates).
    geom = [[[-94.151, 47.4065], [-94.150, 47.406], [-94.149, 47.4055]]]
    feats = [_feat(9131109, geom), _feat(9281824, geom), _feat(9432539, geom)]
    rows = mvum_rows(feats)
    assert len(rows) == 1
    assert rows[0][:3] == ("mvum", stable_mvum_id(geom), "drivable")
    assert rows[0][4] == paths_to_wkt(geom)


def test_mvum_rows_keep_distinct_geometries():
    g1 = [[[-94.1, 47.5], [-94.0, 47.6]]]
    g2 = [[[-94.2, 47.4], [-94.3, 47.3]]]
    rows = mvum_rows([_feat(1, g1), _feat(2, g2)])
    assert len(rows) == 2
    assert {r[1] for r in rows} == {stable_mvum_id(g1), stable_mvum_id(g2)}


def test_mvum_rows_skip_geometryless_features():
    assert mvum_rows([_feat(1, [])]) == []
    assert mvum_rows([_feat(1, [[[-94.1, 47.5]]])]) == []  # single point
