"""Scoring core (FC-5, TLC-1, TLC-6, TLC-8): normalization shapes, hard filter,
weight renormalization, contribution decomposition, deterministic ranking.

Every expected number is derived from DEFAULT_SCORING_PARAMS so the test and the
implementation share one source of truth for the tunable knobs.
"""

from __future__ import annotations

import math

from cairn.engine.scoring import score
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS
from cairn.engine.types import (
    Candidate,
    CandidateFeatures,
    FeatureValue,
    ScoredCandidate,
)

PARAMS = DEFAULT_SCORING_PARAMS
FEAT = PARAMS["features"]

SOFT_DEG = FEAT["flatness"]["soft_deg"]
HARD_DEG = FEAT["flatness"]["hard_deg"]
RISE_START = FEAT["water_proximity"]["rise_start"]
PLATEAU_LO = FEAT["water_proximity"]["plateau_lo"]
PLATEAU_HI = FEAT["water_proximity"]["plateau_hi"]
FALL_END = FEAT["water_proximity"]["fall_end"]
DECAY_M = FEAT["tree_cover"]["decay_m"]
CAP_M = FEAT["solitude"]["cap_m"]


# --- Reference normalization shapes, computed straight from the params. ------


def norm_flatness(slope: float) -> float:
    return max(0.0, min(1.0, 1.0 - slope / SOFT_DEG))


def norm_water(d: float) -> float:
    if d < RISE_START or d >= FALL_END:
        return 0.0
    if d < PLATEAU_LO:
        return (d - RISE_START) / (PLATEAU_LO - RISE_START)
    if d <= PLATEAU_HI:
        return 1.0
    return (FALL_END - d) / (FALL_END - PLATEAU_HI)


def norm_tree(d: float) -> float:
    return math.exp(-d / DECAY_M)


def norm_solitude(d: float) -> float:
    return min(d / CAP_M, 1.0)


def fv(value: float, available: bool = True) -> FeatureValue:
    return FeatureValue(value=value, available=available)


def make_cf(
    lon: float,
    lat: float,
    *,
    slope: float | None = None,
    dist_water: float | None = None,
    dist_forest: float | None = None,
    dist_solitude: float | None = None,
    slope_available: bool = True,
    water_available: bool = True,
    forest_available: bool = True,
    solitude_available: bool = True,
    near_campsite: bool = False,
    way_id: int = 1,
) -> CandidateFeatures:
    features: dict[str, FeatureValue] = {}
    if slope is not None or not slope_available:
        features["flatness"] = fv(slope if slope is not None else 0.0, slope_available)
    if dist_water is not None or not water_available:
        features["water_proximity"] = fv(
            dist_water if dist_water is not None else 0.0, water_available
        )
    if dist_forest is not None or not forest_available:
        features["tree_cover"] = fv(
            dist_forest if dist_forest is not None else 0.0, forest_available
        )
    if dist_solitude is not None or not solitude_available:
        features["solitude"] = fv(
            dist_solitude if dist_solitude is not None else 0.0, solitude_available
        )
    return CandidateFeatures(
        candidate=Candidate(lon=lon, lat=lat, source_way_id=way_id),
        features=features,
        near_campsite=near_campsite,
    )


def contributions_by_feature(sc: ScoredCandidate) -> dict[str, object]:
    return {c.feature: c for c in sc.contributions}


# --- Normalization shapes at several input points. ---------------------------


def test_flatness_linear_decreasing():
    for slope, expected in [(0.0, 1.0), (10.0, 0.5), (20.0, 0.0), (25.0, 0.0)]:
        # slope 20/25 would be hard-filtered when flatness is the only feature,
        # so score a tiny-slope candidate and compare normalized value directly.
        assert math.isclose(norm_flatness(slope), expected, abs_tol=1e-12)
    cf = make_cf(0.0, 0.0, slope=5.0)
    out = score([cf], {"flatness": 1.0}, PARAMS)
    assert len(out) == 1
    c = contributions_by_feature(out[0])["flatness"]
    assert math.isclose(c.normalized, norm_flatness(5.0), abs_tol=1e-9)


def test_water_trapezoid_shape():
    # Lakeside curve (v4 params): rise [0,30], plateau [30,150], fall [150,600].
    for d, expected in [
        (0.0, 0.0),
        (15.0, 0.5),
        (30.0, 1.0),
        (150.0, 1.0),
        (375.0, 0.5),
        (600.0, 0.0),
        (2000.0, 0.0),
    ]:
        assert math.isclose(norm_water(d), expected, abs_tol=1e-9), d
    cf = make_cf(0.0, 0.0, dist_water=375.0)
    out = score([cf], {"water_proximity": 1.0}, PARAMS)
    c = contributions_by_feature(out[0])["water_proximity"]
    assert math.isclose(c.normalized, 0.5, abs_tol=1e-9)


def test_tree_distance_decay():
    assert math.isclose(norm_tree(0.0), 1.0, abs_tol=1e-12)
    assert math.isclose(norm_tree(DECAY_M), 1.0 / math.e, abs_tol=1e-12)
    cf = make_cf(0.0, 0.0, dist_forest=DECAY_M)
    out = score([cf], {"tree_cover": 1.0}, PARAMS)
    c = contributions_by_feature(out[0])["tree_cover"]
    assert math.isclose(c.normalized, 1.0 / math.e, abs_tol=1e-9)


def test_solitude_linear_increasing():
    for d, expected in [(0.0, 0.0), (1500.0, 0.5), (3000.0, 1.0), (5000.0, 1.0)]:
        assert math.isclose(norm_solitude(d), expected, abs_tol=1e-12)
    cf = make_cf(0.0, 0.0, dist_solitude=1500.0)
    out = score([cf], {"solitude": 1.0}, PARAMS)
    c = contributions_by_feature(out[0])["solitude"]
    assert math.isclose(c.normalized, 0.5, abs_tol=1e-9)


# --- Hard filter (TLC-4). ----------------------------------------------------


def test_hard_filter_drops_steep_keeps_gentle():
    steep = make_cf(1.0, 1.0, slope=20.0, dist_water=60.0, way_id=1)  # > 15 -> drop
    gentle = make_cf(2.0, 2.0, slope=5.0, dist_water=60.0, way_id=2)  # keep
    out = score(
        [steep, gentle],
        {"flatness": 0.5, "water_proximity": 0.5},
        PARAMS,
    )
    coords = {(sc.candidate.lon, sc.candidate.lat) for sc in out}
    assert (2.0, 2.0) in coords
    assert (1.0, 1.0) not in coords
    assert len(out) == 1


def test_hard_filter_only_when_slope_available():
    # Unavailable flatness must NOT be treated as a (huge) slope and dropped.
    cf = make_cf(
        0.0, 0.0, slope_available=False, dist_water=60.0, way_id=1
    )
    out = score([cf], {"flatness": 0.5, "water_proximity": 0.5}, PARAMS)
    assert len(out) == 1
    assert "flatness" in out[0].degraded_features


def test_hard_filter_boundary_exactly_at_threshold_is_kept():
    # Spec: dropped iff slope > hard_deg. At exactly hard_deg it is retained.
    cf = make_cf(0.0, 0.0, slope=HARD_DEG, dist_water=60.0)
    out = score([cf], {"flatness": 0.5, "water_proximity": 0.5}, PARAMS)
    assert len(out) == 1


# --- TLC-6: weight renormalization over available, weighted features. --------


def test_renormalization_with_one_feature_unavailable():
    # water unavailable; flatness+tree+solitude weighted -> renormalize over 3.
    cf = make_cf(
        0.0,
        0.0,
        slope=0.0,  # flatness norm = 1
        dist_forest=0.0,  # tree norm = 1
        dist_solitude=CAP_M,  # solitude norm = 1
        water_available=False,
    )
    weights = {
        "flatness": 0.4,
        "water_proximity": 0.3,
        "tree_cover": 0.2,
        "solitude": 0.1,
    }
    out = score([cf], weights, PARAMS)
    assert len(out) == 1
    sc = out[0]
    assert "water_proximity" in sc.degraded_features
    cbf = contributions_by_feature(sc)
    assert "water_proximity" not in cbf
    used = sum(weights[f] for f in ("flatness", "tree_cover", "solitude"))
    for f in ("flatness", "tree_cover", "solitude"):
        assert math.isclose(cbf[f].weight, weights[f] / used, abs_tol=1e-9)
    # Renormalized weights sum to 1.
    assert math.isclose(
        sum(c.weight for c in sc.contributions), 1.0, abs_tol=1e-9
    )
    # All three normalized to 1 -> total 1.
    assert math.isclose(sc.total, 1.0, abs_tol=1e-9)


def test_unavailable_weighted_feature_not_treated_as_zero():
    # If "unavailable == 0" the total would drop; instead it must be excluded.
    full = make_cf(0.0, 0.0, slope=0.0, dist_water=60.0)
    degraded = make_cf(
        1.0, 1.0, slope=0.0, dist_water=0.0, water_available=False
    )
    weights = {"flatness": 0.5, "water_proximity": 0.5}
    out = score([full, degraded], weights, PARAMS)
    by_coord = {(sc.candidate.lon, sc.candidate.lat): sc for sc in out}
    # Degraded candidate scores purely on flatness (norm 1) -> total 1.0,
    # NOT 0.5 (which is what zero-imputing water would give).
    assert math.isclose(by_coord[(1.0, 1.0)].total, 1.0, abs_tol=1e-9)
    assert math.isclose(by_coord[(0.0, 0.0)].total, 1.0, abs_tol=1e-9)


def test_zero_weight_feature_excluded_even_if_available():
    cf = make_cf(0.0, 0.0, slope=10.0, dist_water=60.0)
    out = score([cf], {"flatness": 1.0, "water_proximity": 0.0}, PARAMS)
    cbf = contributions_by_feature(out[0])
    assert "water_proximity" not in cbf
    assert math.isclose(out[0].total, norm_flatness(10.0), abs_tol=1e-9)


# --- TLC-8: contributions sum to total. --------------------------------------


def test_contributions_sum_to_total_for_every_candidate():
    cands = [
        make_cf(0.0, 0.0, slope=5.0, dist_water=100.0, dist_forest=50.0,
                dist_solitude=1000.0, way_id=1),
        make_cf(1.0, 1.0, slope=0.0, dist_water=800.0, dist_forest=300.0,
                dist_solitude=3000.0, way_id=2),
        make_cf(2.0, 2.0, slope=12.0, dist_water=60.0, dist_forest=0.0,
                dist_solitude=200.0, way_id=3),
    ]
    weights = {
        "flatness": 0.4,
        "water_proximity": 0.3,
        "tree_cover": 0.2,
        "solitude": 0.1,
    }
    out = score(cands, weights, PARAMS)
    assert len(out) == 3
    for sc in out:
        summed = sum(c.contribution for c in sc.contributions)
        assert math.isclose(summed, sc.total, abs_tol=1e-9)
        for c in sc.contributions:
            assert math.isclose(
                c.contribution, c.normalized * c.weight, abs_tol=1e-9
            )


def test_total_matches_hand_computed_value():
    cf = make_cf(
        0.0, 0.0, slope=10.0, dist_water=60.0, dist_forest=DECAY_M,
        dist_solitude=CAP_M,
    )
    weights = {
        "flatness": 0.25,
        "water_proximity": 0.25,
        "tree_cover": 0.25,
        "solitude": 0.25,
    }
    out = score([cf], weights, PARAMS)
    # All four available & equal-weight -> renormalized weight 0.25 each.
    expected = 0.25 * (
        norm_flatness(10.0)
        + norm_water(60.0)
        + norm_tree(DECAY_M)
        + norm_solitude(CAP_M)
    )
    assert math.isclose(out[0].total, expected, abs_tol=1e-9)


# --- All weighted features unavailable -> total 0. ---------------------------


def test_all_weighted_unavailable_gives_zero_total():
    cf = make_cf(
        0.0,
        0.0,
        slope_available=False,
        water_available=False,
        forest_available=False,
        solitude_available=False,
    )
    weights = {
        "flatness": 0.4,
        "water_proximity": 0.3,
        "tree_cover": 0.2,
        "solitude": 0.1,
    }
    out = score([cf], weights, PARAMS)
    assert len(out) == 1
    assert math.isclose(out[0].total, 0.0, abs_tol=1e-12)
    assert set(out[0].degraded_features) == {
        "flatness",
        "water_proximity",
        "tree_cover",
        "solitude",
    }
    assert out[0].contributions == []


# --- near_campsite passthrough. ----------------------------------------------


def test_near_campsite_passes_through():
    cf = make_cf(0.0, 0.0, slope=5.0, near_campsite=True)
    out = score([cf], {"flatness": 1.0}, PARAMS)
    assert out[0].near_campsite is True


# --- Ranking: descending by total. -------------------------------------------


def test_output_sorted_descending_by_total():
    low = make_cf(0.0, 0.0, slope=18.0, way_id=1)  # norm 0.1
    mid = make_cf(1.0, 1.0, slope=10.0, way_id=2)  # norm 0.5
    high = make_cf(2.0, 2.0, slope=0.0, way_id=3)  # norm 1.0
    out = score([low, mid, high], {"flatness": 1.0}, PARAMS)
    totals = [sc.total for sc in out]
    assert totals == sorted(totals, reverse=True)
    assert math.isclose(out[0].total, 1.0, abs_tol=1e-9)


# --- TLC-1: determinism and stable tie-break. --------------------------------


def test_scoring_is_deterministic_across_runs():
    cands = [
        make_cf(0.0, 0.0, slope=5.0, dist_water=100.0, dist_forest=50.0,
                dist_solitude=1000.0, way_id=1),
        make_cf(1.0, 2.0, slope=0.0, dist_water=800.0, dist_forest=300.0,
                dist_solitude=3000.0, way_id=2),
        make_cf(-1.0, 0.5, slope=12.0, dist_water=60.0, dist_forest=0.0,
                dist_solitude=200.0, way_id=3),
    ]
    weights = {
        "flatness": 0.4,
        "water_proximity": 0.3,
        "tree_cover": 0.2,
        "solitude": 0.1,
    }
    first = score(cands, weights, PARAMS)
    for _ in range(100):
        again = score(cands, weights, PARAMS)
        assert again == first  # dataclass equality: scores, order, breakdown


def test_equal_totals_have_stable_tiebreak():
    # Two candidates with identical features -> identical totals; their relative
    # order must be fixed and reproducible across runs (don't hard-code winner).
    a = make_cf(5.0, 5.0, slope=10.0, dist_water=60.0, way_id=1)
    b = make_cf(-3.0, 2.0, slope=10.0, dist_water=60.0, way_id=2)
    weights = {"flatness": 0.5, "water_proximity": 0.5}
    out1 = score([a, b], weights, PARAMS)
    out2 = score([b, a], weights, PARAMS)  # input order swapped
    assert math.isclose(out1[0].total, out1[1].total, abs_tol=1e-9)
    order1 = [(sc.candidate.lon, sc.candidate.lat) for sc in out1]
    order2 = [(sc.candidate.lon, sc.candidate.lat) for sc in out2]
    # Tie-break does not depend on input order -> deterministic, stable.
    assert order1 == order2
    # And repeated runs match exactly.
    assert score([a, b], weights, PARAMS) == out1
