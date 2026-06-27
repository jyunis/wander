"""Tests for the road mud-susceptibility core (cairn.roadmud): the
soil/slope/water → base-score formula, surface modifier, percentile aggregation,
tier mapping, polyline densification, the NumPy soil grid, and the reusable
per-road tier. Pure + table-driven; shared by the standalone bake
(scripts/load_road_mud.py) and the live auto-tier hook (cairn.service)."""

from __future__ import annotations

import numpy as np

from cairn import roadmud as lrm


# --- soil scoring ----------------------------------------------------------
def test_drainage_score_table():
    assert lrm._drainage_score(1) == 0.0
    assert lrm._drainage_score(4) == 0.0
    assert lrm._drainage_score(8) == 0.1
    assert lrm._drainage_score(2) == 0.3
    assert lrm._drainage_score(5) == 0.6
    assert lrm._drainage_score(3) == 0.85
    assert lrm._drainage_score(7) == 1.0
    assert lrm._drainage_score(6) == 1.0
    assert lrm._drainage_score(0) is None
    assert lrm._drainage_score(9) is None
    assert lrm._drainage_score(None) is None


def test_hsg_score_dual_groups_use_wet_letter():
    assert lrm._hsg_score(1) == 0.0
    assert lrm._hsg_score(3) == 0.25
    assert lrm._hsg_score(5) == 0.6
    assert lrm._hsg_score(7) == 1.0
    assert lrm._hsg_score(2) == 1.0  # A/D -> D
    assert lrm._hsg_score(4) == 1.0  # B/D -> D
    assert lrm._hsg_score(6) == 1.0  # C/D -> D
    assert lrm._hsg_score(0) is None
    assert lrm._hsg_score(8) is None


def test_soil_score_takes_the_worse_and_handles_missing():
    assert lrm._soil_score(8, 7) == 1.0   # well-drained but HSG D -> soft wins
    assert lrm._soil_score(1, 1) == 0.0
    assert lrm._soil_score(0, 5) == 0.6   # drainage NA, HSG C
    assert lrm._soil_score(3, 0) == 0.85
    assert lrm._soil_score(0, 0) is None


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
    assert lrm._water_factor(None) == 0.0


# --- base score ------------------------------------------------------------
def test_base_score_all_present_matches_spec_weights():
    assert lrm._base_score(1.0, 1.0, 1.0) == 1.0
    assert lrm._base_score(0.0, 0.0, 0.0) == 0.0
    assert abs(lrm._base_score(1.0, 0.0, 0.0) - 0.60) < 1e-9
    assert abs(lrm._base_score(0.5, 0.2, 1.0) - (0.6 * 0.5 + 0.25 * 0.2 + 0.15 * 1.0)) < 1e-9


def test_base_score_renormalizes_when_slope_missing():
    assert abs(lrm._base_score(1.0, None, 0.0) - (0.60 / 0.75)) < 1e-9
    assert abs(lrm._base_score(0.5, None, 0.5) - 0.5) < 1e-9


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
    assert lrm._surface_class("dirt/sand") == "soft"
    assert lrm._surface_class("natural") == "soft"
    assert lrm._surface_class("unpaved") == "unknown"  # contains 'paved' but isn't pavement
    assert lrm._surface_class("sand") == "unknown"
    assert lrm._surface_class("") == "unknown"
    assert lrm._surface_class(None) == "unknown"


# --- percentile aggregation ------------------------------------------------
def test_percentile_linear_interpolation():
    assert lrm._percentile([], 0.85) is None
    assert lrm._percentile([0.42], 0.85) == 0.42
    assert lrm._percentile([0.0, 1.0], 0.5) == 0.5
    vals = [float(i) for i in range(11)]
    assert abs(lrm._percentile(vals, 0.85) - 8.5) < 1e-9


def test_percentile_is_robust_to_a_single_noisy_pixel():
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
    assert lrm._segment_tier([], "asphalt") == "Firm"
    assert lrm._segment_tier([0.9, 0.95], "paved") == "Firm"


def test_segment_tier_no_data_when_no_points():
    assert lrm._segment_tier([], None) is None
    assert lrm._segment_tier([], "gravel") is None


def test_segment_tier_surface_multiplier_can_shift_a_tier():
    bases = [0.55] * 10
    assert lrm._segment_tier(bases, "unknown") == "Variable"
    assert lrm._segment_tier(bases, "dirt") == "Soft"      # ×1.15
    assert lrm._segment_tier([0.40] * 10, "gravel") == "Firm"  # ×0.70


def test_worst_tier():
    assert lrm.worst_tier(["Firm", "Soft", "Variable"]) == "Soft"
    assert lrm.worst_tier(["Firm", "Variable"]) == "Variable"
    assert lrm.worst_tier(["Firm", None]) == "Firm"
    assert lrm.worst_tier([None, None]) is None
    assert lrm.worst_tier([]) is None


# --- densification ---------------------------------------------------------
def test_densify_short_segment_returns_endpoints():
    pts = lrm._densify([(-93.0, 45.0), (-93.0, 45.0001)], step_m=75.0)
    assert pts[0] == (-93.0, 45.0)
    assert pts[-1] == (-93.0, 45.0001)
    assert len(pts) == 2


def test_densify_spacing_and_endpoint_kept():
    start = (-93.0, 45.0)
    end = (-93.0, 45.0 + 1000.0 / 110540.0)
    pts = lrm._densify([start, end], step_m=100.0)
    assert pts[0] == start and pts[-1] == end
    assert 9 <= len(pts) <= 13
    gaps = [lrm._haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:])]
    assert all(g <= 100.0 + 1.0 for g in gaps)


def test_densify_single_point():
    assert lrm._densify([(-93.0, 45.0)]) == [(-93.0, 45.0)]


def test_line_coords_parts():
    assert lrm.line_coords({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}) == [
        [(0.0, 0.0), (1.0, 1.0)]
    ]
    ml = {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]}
    assert len(lrm.line_coords(ml)) == 2
    assert lrm.line_coords({"type": "Point", "coordinates": [0, 0]}) == []


# --- NumPy soil grid -------------------------------------------------------
def test_soil_grid_samples_north_up_and_oob():
    # 2×2 grid over (w,s,e,n)=(0,0,2,2). Row 0 is the NORTH edge.
    drainage = np.array([[8, 3], [1, 7]], dtype=np.uint8)
    hsg = np.array([[1, 5], [3, 7]], dtype=np.uint8)
    g = lrm.SoilGrid(drainage, hsg, (0, 0, 2, 2))
    d, h = g.codes([0.5], [1.5])  # NW cell
    assert d == [8] and h == [1]
    d, h = g.codes([1.5], [0.5])  # SE cell
    assert d == [7] and h == [7]
    d, h = g.codes([5.0], [5.0])  # out of grid -> 0 (None downstream)
    assert d == [0] and h == [0]


# --- the reusable per-road tier (async) ------------------------------------
async def test_tier_road_combines_signals():
    drainage = np.full((1, 1), 3, np.uint8)  # poorly drained -> 0.85
    hsg = np.full((1, 1), 7, np.uint8)        # D -> 1.0 ; soil = max -> 1.0
    g = lrm.SoilGrid(drainage, hsg, (0, 0, 1, 1))

    async def slope_fn(lon, lat):
        return 0.0  # flat -> slope_factor 1.0

    async def water_fn(pts):
        return [1000.0] * len(pts)  # far -> water_factor 0

    # base = 0.60*1.0 + 0.25*1.0 + 0.15*0 = 0.85 -> Soft
    tier = await lrm.tier_road([(0.2, 0.2), (0.8, 0.8)], None, g, slope_fn, water_fn)
    assert tier == "Soft"


async def test_tier_road_none_when_no_soil():
    g = lrm.SoilGrid(np.zeros((1, 1), np.uint8), np.zeros((1, 1), np.uint8), (0, 0, 1, 1))

    async def slope_fn(lon, lat):
        return 2.0

    async def water_fn(pts):
        return [None] * len(pts)

    # No soil anywhere -> None (unless paved). Paved short-circuits to Firm.
    assert await lrm.tier_road([(0.2, 0.2), (0.8, 0.8)], None, g, slope_fn, water_fn) is None
    assert await lrm.tier_road([(0.2, 0.2), (0.8, 0.8)], "asphalt", g, slope_fn, water_fn) == "Firm"


async def test_tier_road_slope_failure_degrades():
    drainage = np.full((1, 1), 5, np.uint8)  # somewhat poorly -> 0.6
    hsg = np.zeros((1, 1), np.uint8)
    g = lrm.SoilGrid(drainage, hsg, (0, 0, 1, 1))

    async def slope_fn(lon, lat):
        raise RuntimeError("tile unreachable")

    async def water_fn(pts):
        return [None] * len(pts)

    # Slope unavailable -> renormalize over the PRESENT terms soil(0.6) + water(0.0,
    # far): base = (0.60*0.6 + 0.15*0.0)/(0.60+0.15) = 0.48 -> Variable.
    tier = await lrm.tier_road([(0.2, 0.2), (0.8, 0.8)], None, g, slope_fn, water_fn)
    assert tier == "Variable"
