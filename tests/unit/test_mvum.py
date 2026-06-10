"""MVUM ingest helpers: tag synthesis + geometry WKT."""

from __future__ import annotations

from cairn.engine.features import compute_road_score, road_is_rough
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS
from cairn.ingest.mvum import MVUM_ID_OFFSET, mvum_tags, paths_to_wkt

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
