"""Candidate generation (FC-3, UoW-5): spacing, grid dedup, cap, determinism.

All expectations are computed with the same equirectangular meters approximation
the spec mandates (Δlat·111320 m/deg; Δlon·111320·cos(lat) m/deg) so the test and
the implementation share one notion of distance. Coordinates sit near Ely, MN.
"""

from __future__ import annotations

import math

import pytest

from cairn.engine.candidates import generate_candidates
from cairn.engine.types import Candidate, Way

# Reference point near Ely, MN.
LAT0 = 47.9
LON0 = -91.86
M_PER_DEG = 111320.0


def lon_for_east_meters(meters: float, lat: float = LAT0) -> float:
    """Longitude offset (east of LON0) that is `meters` away at latitude `lat`."""
    return LON0 + meters / (M_PER_DEG * math.cos(math.radians(lat)))


def lat_for_north_meters(meters: float) -> float:
    """Latitude offset (north of LAT0) that is `meters` away."""
    return LAT0 + meters / M_PER_DEG


def equirect_dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular distance between two (lon, lat) points (spec approximation)."""
    lon_a, lat_a = a
    lon_b, lat_b = b
    mean_lat = math.radians((lat_a + lat_b) / 2.0)
    dx = (lon_b - lon_a) * M_PER_DEG * math.cos(mean_lat)
    dy = (lat_b - lat_a) * M_PER_DEG
    return math.hypot(dx, dy)


def straight_east_way(osm_id: int, length_m: float, lat: float = LAT0) -> Way:
    """A two-vertex way running due east from (LON0, lat) for `length_m` meters."""
    return Way(
        osm_id=osm_id,
        coords=((LON0, lat), (lon_for_east_meters(length_m, lat), lat)),
    )


def test_empty_ways_returns_empty_and_uncapped():
    candidates, capped = generate_candidates(
        [], spacing_m=50.0, grid_m=10.0, max_candidates=1000
    )
    assert candidates == []
    assert capped is False


def test_straight_way_known_point_count_and_spacing():
    # 1000 m way, 100 m spacing -> roughly floor(1000/100)+1 = 11 points.
    way = straight_east_way(1, length_m=1000.0)
    candidates, capped = generate_candidates(
        [way], spacing_m=100.0, grid_m=1.0, max_candidates=10_000
    )
    assert capped is False
    # Don't over-pin the boundary: accept the dominant 11, tolerate ±1.
    assert 10 <= len(candidates) <= 12
    # Consecutive emitted points should be ~spacing_m apart.
    pts = [(c.lon, c.lat) for c in candidates]
    for a, b in zip(pts, pts[1:]):
        d = equirect_dist_m(a, b)
        assert math.isclose(d, 100.0, rel_tol=0.1), d
    # Endpoints of the way are included (first point at the start vertex).
    assert math.isclose(candidates[0].lon, LON0, abs_tol=1e-9)
    assert math.isclose(candidates[0].lat, LAT0, abs_tol=1e-9)
    # Source way id is carried through.
    assert all(c.source_way_id == 1 for c in candidates)


def test_points_are_candidate_instances():
    way = straight_east_way(7, length_m=300.0)
    candidates, _ = generate_candidates(
        [way], spacing_m=100.0, grid_m=1.0, max_candidates=10_000
    )
    assert candidates
    assert all(isinstance(c, Candidate) for c in candidates)


def test_grid_dedup_collapses_overlapping_ways():
    # Two identical ways: every sampled point of way B snaps onto way A's grid
    # cells, so the deduped result should match a single way's output count.
    way_a = straight_east_way(1, length_m=1000.0)
    way_b = straight_east_way(2, length_m=1000.0)
    one, _ = generate_candidates(
        [way_a], spacing_m=100.0, grid_m=50.0, max_candidates=10_000
    )
    both, _ = generate_candidates(
        [way_a, way_b], spacing_m=100.0, grid_m=50.0, max_candidates=10_000
    )
    assert len(both) == len(one)


def test_grid_dedup_collapses_closely_spaced_points():
    # A coarse grid (200 m) relative to spacing (50 m) collapses multiple
    # consecutive samples into the same cell, reducing the point count.
    way = straight_east_way(1, length_m=1000.0)
    fine, _ = generate_candidates(
        [way], spacing_m=50.0, grid_m=1.0, max_candidates=10_000
    )
    coarse, _ = generate_candidates(
        [way], spacing_m=50.0, grid_m=200.0, max_candidates=10_000
    )
    assert len(coarse) < len(fine)
    # Surviving points must be at least ~one grid cell apart.
    pts = [(c.lon, c.lat) for c in coarse]
    for a, b in zip(pts, pts[1:]):
        assert equirect_dist_m(a, b) >= 200.0 * 0.5


def test_determinism_same_input_same_output():
    ways = [
        straight_east_way(1, length_m=800.0),
        straight_east_way(2, length_m=500.0, lat=lat_for_north_meters(500.0)),
    ]
    first, capped1 = generate_candidates(
        ways, spacing_m=75.0, grid_m=20.0, max_candidates=10_000
    )
    second, capped2 = generate_candidates(
        ways, spacing_m=75.0, grid_m=20.0, max_candidates=10_000
    )
    assert capped1 == capped2
    assert first == second  # same points, same order


def test_cap_hit_truncates_and_flags():
    # 5000 m / 50 m spacing -> ~101 points; cap at 10.
    way = straight_east_way(1, length_m=5000.0)
    candidates, capped = generate_candidates(
        [way], spacing_m=50.0, grid_m=1.0, max_candidates=10
    )
    assert capped is True
    assert len(candidates) == 10


def test_under_cap_not_flagged():
    way = straight_east_way(1, length_m=300.0)
    candidates, capped = generate_candidates(
        [way], spacing_m=100.0, grid_m=1.0, max_candidates=10_000
    )
    assert capped is False
    assert len(candidates) <= 10_000


def test_cap_truncation_is_deterministic():
    way = straight_east_way(1, length_m=5000.0)
    a, capped_a = generate_candidates(
        [way], spacing_m=50.0, grid_m=1.0, max_candidates=10
    )
    b, capped_b = generate_candidates(
        [way], spacing_m=50.0, grid_m=1.0, max_candidates=10
    )
    assert capped_a is True and capped_b is True
    assert a == b


@pytest.mark.parametrize("length_m,spacing_m", [(500.0, 100.0), (1000.0, 250.0)])
def test_endpoints_within_way_bounds(length_m: float, spacing_m: float):
    way = straight_east_way(1, length_m=length_m)
    candidates, _ = generate_candidates(
        [way], spacing_m=spacing_m, grid_m=1.0, max_candidates=10_000
    )
    end_lon = lon_for_east_meters(length_m)
    for c in candidates:
        # Every point lies on the (east-running) segment, within its lon span.
        assert LON0 - 1e-9 <= c.lon <= end_lon + 1e-9
        assert math.isclose(c.lat, LAT0, abs_tol=1e-9)
