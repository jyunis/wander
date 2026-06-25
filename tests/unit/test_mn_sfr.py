"""MN state-forest-roads ingest helpers: tag synthesis + id keying.

Mirrors test_mvum.py — the same drivable-way contract (full forest-road
road_character, informational rough flag) applies, with MN DNR road classes."""

from __future__ import annotations

from cairn.engine.features import compute_road_score, road_is_rough
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS
from cairn.ingest.mn_sfr import MN_SFR_ID_OFFSET, mn_sfr_tags
from cairn.ingest.mvum import MVUM_ID_OFFSET

ROAD_CFG = DEFAULT_SCORING_PARAMS["features"]["road_character"]


def test_minimum_maintenance_road_is_forest_track_and_rough():
    tags = mn_sfr_tags(
        {"objectid": 1, "road_num": "0010A", "road_name": "0010A",
         "road_class": "Minimum Maintenance Road"}
    )
    assert tags["highway"] == "track"
    assert tags["operator"] == "MN DNR"
    assert tags["source"] == "mn_sfr"
    assert tags["ref"] == "0010A"
    assert tags["mn_sfr_class"] == "Minimum Maintenance Road"
    # A forest track scores full road_character (not a penalty); minimally
    # maintained -> informational rough flag (smoothness=very_bad).
    assert compute_road_score(tags, ROAD_CFG) == 1.0
    assert road_is_rough(tags) is True


def test_system_forest_road_is_drivable_but_not_rough():
    tags = mn_sfr_tags(
        {"objectid": 2, "road_num": "0042", "road_name": "Pillsbury Tote Rd",
         "road_class": "System Forest Road"}
    )
    assert tags["name"] == "Pillsbury Tote Rd"
    assert "smoothness" not in tags  # maintained -> no rough flag
    assert road_is_rough(tags) is False
    assert compute_road_score(tags, ROAD_CFG) == 1.0


def test_blank_class_still_yields_a_drivable_track():
    tags = mn_sfr_tags({"objectid": 3, "road_num": "9", "road_name": "", "road_class": None})
    assert tags["highway"] == "track"
    assert "mn_sfr_class" not in tags
    assert "smoothness" not in tags
    assert compute_road_score(tags, ROAD_CFG) == 1.0


def test_id_offset_disjoint_from_osm_and_mvum():
    # MN SFR ids sit in a band above the MVUM band, both far above OSM way ids;
    # the 1e15 gap dwarfs any plausible objectid, so the ranges never collide.
    assert MN_SFR_ID_OFFSET > MVUM_ID_OFFSET
    assert MN_SFR_ID_OFFSET - MVUM_ID_OFFSET > 10**12
