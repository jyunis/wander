"""Spatial non-maximum suppression — distinct-site selection (engine.scoring)."""

from __future__ import annotations

from cairn.engine.scoring import _distance_m, select_distinct
from cairn.engine.types import Candidate, ScoredCandidate

# ~111.32 m per 0.001° latitude; use latitude offsets for predictable meters.
LAT0, LON0 = 47.90, -91.86


def _sc(total, dlat_m=0.0, dlon_m=0.0, way=1):
    lat = LAT0 + dlat_m / 111320.0
    lon = LON0 + dlon_m / (111320.0 * 0.671)  # cos(47.9°) ~ 0.671
    return ScoredCandidate(
        candidate=Candidate(lon=lon, lat=lat, source_way_id=way),
        total=total,
        contributions=[],
    )


def _ranked(scs):
    return sorted(scs, key=lambda s: -s.total)


def test_distance_helper_is_meters():
    a = Candidate(LON0, LAT0, 1)
    b = Candidate(LON0, LAT0 + 100 / 111320.0, 1)
    assert abs(_distance_m(a, b) - 100) < 1.0


def test_suppresses_lower_neighbor_within_radius():
    # Two points 200 m apart; radius 400 m -> only the higher survives.
    high = _sc(0.9, dlat_m=0)
    low = _sc(0.5, dlat_m=200)
    out = select_distinct(_ranked([high, low]), radius_m=400)
    by_total = {round(s.total, 1): s for s in out}
    assert by_total[0.9].distinct is True
    assert by_total[0.9].distinct_rank == 1
    assert by_total[0.5].distinct is False
    assert by_total[0.5].distinct_rank is None


def test_keeps_distant_points_as_separate_sites():
    a = _sc(0.9, dlat_m=0)
    b = _sc(0.8, dlat_m=1000)  # 1 km away
    out = select_distinct(_ranked([a, b]), radius_m=400)
    assert all(s.distinct for s in out)
    assert sorted(s.distinct_rank for s in out) == [1, 2]


def test_always_keeps_the_highest_scoring():
    pts = [_sc(0.9 - i * 0.05, dlat_m=i * 50) for i in range(6)]  # tight cluster
    out = select_distinct(_ranked(pts), radius_m=500)
    top = max(out, key=lambda s: s.total)
    assert top.distinct and top.distinct_rank == 1
    # The cluster collapses to a single distinct site.
    assert sum(1 for s in out if s.distinct) == 1


def test_distinct_rank_is_dense_and_score_ordered():
    pts = [_sc(0.9, 0), _sc(0.7, 1000), _sc(0.5, 2000)]
    out = select_distinct(_ranked(pts), radius_m=300)
    distinct = sorted((s for s in out if s.distinct), key=lambda s: s.distinct_rank)
    assert [s.distinct_rank for s in distinct] == [1, 2, 3]
    assert [round(s.total, 1) for s in distinct] == [0.9, 0.7, 0.5]


def test_radius_zero_keeps_all():
    pts = [_sc(0.9, 0), _sc(0.8, 1), _sc(0.7, 2)]  # essentially coincident
    out = select_distinct(_ranked(pts), radius_m=0)
    assert all(s.distinct for s in out)


def test_deterministic():
    pts = [_sc(0.9 - i * 0.03, dlat_m=i * 120, way=i) for i in range(20)]
    r = _ranked(pts)
    a = select_distinct(r, radius_m=350)
    b = select_distinct(r, radius_m=350)
    assert a == b
