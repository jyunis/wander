"""road_character feature: source-way tags -> [0,1] forest-road score (FC-4)."""

from __future__ import annotations

import pytest

from cairn.engine.features import compute_road_score
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS

CFG = DEFAULT_SCORING_PARAMS["features"]["road_character"]


@pytest.mark.parametrize(
    "tags,expected",
    [
        ({"highway": "track"}, 1.0),
        ({"highway": "unclassified"}, 0.75),
        ({"highway": "service"}, 0.5),
        ({"highway": "residential"}, 0.25),
        ({"highway": "tertiary"}, 0.15),
        ({"highway": "secondary"}, 0.3),  # not a candidate class -> _default
        ({}, 0.3),
    ],
)
def test_class_scores(tags, expected):
    assert compute_road_score(tags, CFG) == pytest.approx(expected)


def test_forest_named_road_floors_to_high():
    # A humble service road that is actually a Forest Service road.
    assert compute_road_score(
        {"highway": "service", "name": "Forest Road 2199"}, CFG
    ) == pytest.approx(1.0)
    assert compute_road_score(
        {"highway": "unclassified", "ref": "FR 2240"}, CFG
    ) == pytest.approx(1.0)
    assert compute_road_score(
        {"highway": "service", "operator": "US Forest Service"}, CFG
    ) == pytest.approx(1.0)


def test_ordinary_unpaved_is_not_penalized():
    # Gravel/dirt forest roads are the TARGET — no roughness penalty.
    assert compute_road_score({"highway": "track", "surface": "gravel"}, CFG) == 1.0
    assert compute_road_score({"highway": "track", "surface": "dirt"}, CFG) == 1.0


def test_only_genuinely_rough_gets_a_mild_ding():
    assert compute_road_score({"highway": "track", "4wd_only": "yes"}, CFG) == pytest.approx(0.9)
    assert compute_road_score(
        {"highway": "track", "tracktype": "grade5"}, CFG
    ) == pytest.approx(0.9)
    assert compute_road_score(
        {"highway": "track", "smoothness": "impassable"}, CFG
    ) == pytest.approx(0.8)
