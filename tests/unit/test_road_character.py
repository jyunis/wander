"""road_character feature: source-way tags -> [0,1] forest-road score (FC-4)."""

from __future__ import annotations

import pytest

from cairn.engine.features import compute_road_score, road_is_rough
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


def test_roughness_never_penalizes_score():
    # Rough access should INCREASE hits, not cost them — a rough forest track
    # is still a full hit (it's flagged separately, not scored down).
    assert compute_road_score({"highway": "track", "surface": "gravel"}, CFG) == 1.0
    assert compute_road_score({"highway": "track", "4wd_only": "yes"}, CFG) == 1.0
    assert compute_road_score({"highway": "track", "tracktype": "grade5"}, CFG) == 1.0
    assert compute_road_score({"highway": "track", "smoothness": "impassable"}, CFG) == 1.0


def test_rough_flag_only_for_genuinely_rough():
    assert road_is_rough({"highway": "track"}) is False
    assert road_is_rough({"highway": "track", "surface": "gravel"}) is False  # normal FR
    assert road_is_rough({"highway": "track", "surface": "dirt"}) is False
    assert road_is_rough({"highway": "track", "4wd_only": "yes"}) is True
    assert road_is_rough({"highway": "track", "tracktype": "grade4"}) is True
    assert road_is_rough({"highway": "track", "tracktype": "grade5"}) is True
    assert road_is_rough({"highway": "track", "smoothness": "very_bad"}) is True
