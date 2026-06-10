"""Hard-disqualified candidates (e.g. building setback) are dropped (TLC-4)."""

from __future__ import annotations

from cairn.engine.scoring import score
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS
from cairn.engine.types import Candidate, CandidateFeatures, FeatureValue

PARAMS = DEFAULT_SCORING_PARAMS
W = {"road_character": 1.0}


def _cf(way, road, disqualified):
    return CandidateFeatures(
        candidate=Candidate(lon=0.0 + way * 1e-3, lat=0.0, source_way_id=way),
        features={"road_character": FeatureValue(road, True)},
        disqualified=disqualified,
    )


def test_disqualified_candidate_is_dropped():
    cands = [_cf(1, 1.0, disqualified=True), _cf(2, 0.5, disqualified=False)]
    out = score(cands, W, PARAMS)
    assert len(out) == 1
    assert out[0].candidate.source_way_id == 2  # the non-disqualified one


def test_all_disqualified_yields_empty():
    out = score([_cf(1, 1.0, True), _cf(2, 0.9, True)], W, PARAMS)
    assert out == []
