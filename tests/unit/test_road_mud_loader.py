"""Pure-helper tests for the road mud-susceptibility bake
(scripts/load_road_mud.py): the soil/slope/water → base-score formula, the
surface modifier, percentile aggregation, the tier mapping, and polyline
densification. Table-driven, like test_cell_coverage_loader.py.

Loaded by file path because scripts/ isn't an installed package and the loader's
GIS deps (rasterio/shapely) are import-guarded — these helpers use only the
stdlib, so they import + run without those deps.
"""

from __future__ import annotations

import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "load_road_mud",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "load_road_mud.py"),
)
lrm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lrm)


# --- soil scoring ----------------------------------------------------------
def test_drainage_score_table():
    # Wetter classes score higher; excessively/somewhat-excessively are firm.
    assert lrm._drainage_score(1) == 0.0   # excessively
    assert lrm._drainage_score(4) == 0.0   # somewhat excessively
    assert lrm._drainage_score(8) == 0.1   # well
    assert lrm._drainage_score(2) == 0.3   # moderately well
    assert lrm._drainage_score(5) == 0.6   # somewhat poorly
    assert lrm._drainage_score(3) == 0.85  # poorly
    assert lrm._drainage_score(7) == 1.0   # very poorly
    assert lrm._drainage_score(6) == 1.0   # subaqueous


def test_drainage_score_na_and_blank():
    assert lrm._drainage_score(0) is None  # NA
    assert lrm._drainage_score(9) is None  # blank (not in the table)
    assert lrm._drainage_score(None) is None


def test_hsg_score_dual_groups_use_wet_letter():
    assert lrm._hsg_score(1) == 0.0    # A
    assert lrm._hsg_score(3) == 0.25   # B
    assert lrm._hsg_score(5) == 0.6    # C
    assert lrm._hsg_score(7) == 1.0    # D
    # Dual A/D, B/D, C/D all behave as the undrained (D) condition.
    assert lrm._hsg_score(2) == 1.0    # A/D
    assert lrm._hsg_score(4) == 1.0    # B/D
    assert lrm._hsg_score(6) == 1.0    # C/D
    assert lrm._hsg_score(0) is None   # NA
    assert lrm._hsg_score(8) is None   # blank


def test_soil_score_takes_the_worse_and_handles_missing():
    # Corroborating signals: take the WORSE (max) of whichever resolve.
    assert lrm._soil_score(8, 7) == 1.0   # well-drained but HSG D -> soft wins
    assert lrm._soil_score(1, 1) == 0.0   # excessively + A -> firm
    assert lrm._soil_score(0, 5) == 0.6   # drainage NA, HSG C -> C
    assert lrm._soil_score(3, 0) == 0.85  # poorly, HSG NA -> poorly
    assert lrm._soil_score(0, 0) is None  # neither resolves


# --- slope + water factors -------------------------------------------------
def test_slope_factor_bands():
    assert lrm._slope_factor(0.0) == 1.0
    assert lrm._slope_factor(0.99) == 1.0
    assert lrm._slope_factor(1.0) == 0.6
    assert lrm._slope_factor(2.9) == 0.6
    assert lrm._slope_factor(3.0) == 0.3
    assert lrm._slope_factor(5.9) == 0.3
    assert lrm._slope_factor(6.0) == 0.1
    assert lrm._slope_factor(45.0) == 0.1


def test_water_factor_bands():
    assert lrm._water_factor(10.0) == 1.0
    assert lrm._water_factor(49.9) == 1.0
    assert lrm._water_factor(50.0) == 0.6
    assert lrm._water_factor(149.9) == 0.6
    assert lrm._water_factor(150.0) == 0.3
    assert lrm._water_factor(399.9) == 0.3
    assert lrm._water_factor(400.0) == 0.0
    assert lrm._water_factor(None) == 0.0  # no water found nearby


# --- base score (weights + degrade-don't-fail renormalization) -------------
def test_base_score_all_present_matches_spec_weights():
    # 0.60*soil + 0.25*slope + 0.15*water with all three present.
    b = lrm._base_score(1.0, 1.0, 1.0)
    assert b == 1.0
    b = lrm._base_score(0.0, 0.0, 0.0)
    assert b == 0.0
    b = lrm._base_score(1.0, 0.0, 0.0)
    assert abs(b - 0.60) < 1e-9
    b = lrm._base_score(0.5, 0.2, 1.0)
    assert abs(b - (0.60 * 0.5 + 0.25 * 0.2 + 0.15 * 1.0)) < 1e-9


def test_base_score_renormalizes_when_slope_missing():
    # Slope unavailable -> weights renormalize over soil+water (0.60 + 0.15).
    b = lrm._base_score(1.0, None, 0.0)
    assert abs(b - (0.60 / 0.75)) < 1e-9
    b = lrm._base_score(0.5, None, 0.5)
    assert abs(b - 0.5) < 1e-9


def test_base_score_none_when_nothing_present():
    assert lrm._base_score(None, None, None) is None


# --- surface modifier ------------------------------------------------------
def test_surface_class_buckets():
    assert lrm._surface_class("asphalt") == "paved"
    assert lrm._surface_class("concrete") == "paved"
    assert lrm._surface_class("paved") == "paved"
    assert lrm._surface_class("gravel") == "firm"
    assert lrm._surface_class("compacted") == "firm"
    assert lrm._surface_class("fine_gravel") == "firm"
    assert lrm._surface_class("ground") == "soft"
    assert lrm._surface_class("dirt") == "soft"
    assert lrm._surface_class("dirt/sand") == "soft"  # contains 'dirt'
    assert lrm._surface_class("natural") == "soft"
    # 'unpaved' contains 'paved' but is NOT pavement — must not read as paved.
    assert lrm._surface_class("unpaved") == "unknown"
    assert lrm._surface_class("sand") == "unknown"
    assert lrm._surface_class("") == "unknown"
    assert lrm._surface_class(None) == "unknown"


# --- percentile aggregation ------------------------------------------------
def test_percentile_linear_interpolation():
    assert lrm._percentile([], 0.85) is None
    assert lrm._percentile([0.42], 0.85) == 0.42
    assert lrm._percentile([0.0, 1.0], 0.5) == 0.5
    assert lrm._percentile([0.0, 1.0], 0.0) == 0.0
    assert lrm._percentile([0.0, 1.0], 1.0) == 1.0
    # 85th percentile of 0..10 (eleven values) lands exactly on 8.5.
    vals = [float(i) for i in range(11)]
    assert abs(lrm._percentile(vals, 0.85) - 8.5) < 1e-9


def test_percentile_is_robust_to_a_single_noisy_pixel():
    # Mostly firm with one soft outlier -> 85th pctile stays firm-ish (the
    # outlier doesn't dominate, unlike a max).
    vals = [0.1] * 19 + [1.0]
    assert lrm._percentile(vals, 0.85) < 0.3


# --- tier mapping ----------------------------------------------------------
def test_tier_for_score_cutpoints():
    assert lrm._tier_for_score(0.0) == "Firm"
    assert lrm._tier_for_score(0.29) == "Firm"
    assert lrm._tier_for_score(0.30) == "Variable"
    assert lrm._tier_for_score(0.59) == "Variable"
    assert lrm._tier_for_score(0.60) == "Soft"
    assert lrm._tier_for_score(1.0) == "Soft"


def test_segment_tier_paved_forces_firm_without_soil():
    # Paved never soups — Firm even when no soil points resolved.
    assert lrm._segment_tier([], "asphalt") == "Firm"
    assert lrm._segment_tier([0.9, 0.95], "paved") == "Firm"


def test_segment_tier_no_data_when_no_points():
    assert lrm._segment_tier([], None) is None
    assert lrm._segment_tier([], "gravel") is None


def test_segment_tier_surface_multiplier_can_shift_a_tier():
    bases = [0.55] * 10  # 85th pctile = 0.55 -> Variable at ×1.0
    assert lrm._segment_tier(bases, "unknown") == "Variable"
    # Native dirt ×1.15 -> 0.6325 -> Soft.
    assert lrm._segment_tier(bases, "dirt") == "Soft"
    # Gravel ×0.70 -> 0.385 -> Variable; a firmer aggregate drops to Firm.
    assert lrm._segment_tier([0.40] * 10, "gravel") == "Firm"


def test_segment_tier_clamps_at_one():
    # ×1.15 on an already-high aggregate stays valid (clamped) and reads Soft.
    assert lrm._segment_tier([1.0] * 5, "ground") == "Soft"


# --- densification ---------------------------------------------------------
def test_densify_short_segment_returns_endpoints():
    # Two points ~16 m apart (well under STEP_M) -> just the endpoints.
    pts = lrm._densify([(-93.0, 45.0), (-93.0, 45.0001)], step_m=75.0)
    assert pts[0] == (-93.0, 45.0)
    assert pts[-1] == (-93.0, 45.0001)
    assert len(pts) == 2


def test_densify_spacing_and_endpoint_kept():
    # A ~1 km north line at 100 m spacing -> ~11 points, ends preserved, spacing
    # close to the target.
    start = (-93.0, 45.0)
    end = (-93.0, 45.0 + 1000.0 / 110540.0)  # ~1 km north
    pts = lrm._densify([start, end], step_m=100.0)
    assert pts[0] == start
    assert pts[-1] == end
    assert 9 <= len(pts) <= 13
    gaps = [
        lrm._haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])
    ]
    # Every interior gap is <= the step (the final partial gap may be shorter).
    assert all(g <= 100.0 + 1.0 for g in gaps)


def test_densify_single_point():
    assert lrm._densify([(-93.0, 45.0)]) == [(-93.0, 45.0)]
