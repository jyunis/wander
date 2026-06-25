"""Terminus injection (_inject_termini): a dead-end snaps the candidate onto the
spur-end, replacing a co-located uniform sample rather than being dropped."""

from __future__ import annotations

import math

from cairn.engine.candidates import grid_cell
from cairn.engine.types import Candidate
from cairn.service import _inject_termini

LAT0, LON0, M_PER_DEG = 47.9, -91.86, 111320.0
GRID_M = 1000.0  # coarse grid so the same-cell setup is unambiguous


def _east(meters: float) -> float:
    return LON0 + meters / (M_PER_DEG * math.cos(math.radians(LAT0)))


def _cand(lon, lat, way_id=1):
    return Candidate(lon=lon, lat=lat, source_way_id=way_id)


def test_terminus_replaces_a_colocated_sample():
    sample = _cand(LON0, LAT0, way_id=10)
    term_lon, term_lat, term_way = _east(40.0), LAT0, 99  # ~40 m east, same cell
    assert grid_cell(LON0, LAT0, GRID_M) == grid_cell(term_lon, term_lat, GRID_M)

    out = _inject_termini([sample], [(term_lon, term_lat, term_way)], GRID_M, max_candidates=5000)

    assert len(out) == 1  # replaced, not appended
    assert (out[0].lon, out[0].lat) == (term_lon, term_lat)  # snapped onto the dead-end
    assert out[0].source_way_id == term_way


def test_terminus_in_empty_cell_is_appended():
    sample = _cand(LON0, LAT0, way_id=10)
    far = (_east(4000.0), LAT0, 7)  # far away -> different cell
    assert grid_cell(LON0, LAT0, GRID_M) != grid_cell(far[0], far[1], GRID_M)

    out = _inject_termini([sample], [far], GRID_M, max_candidates=5000)

    assert len(out) == 2
    assert (out[1].lon, out[1].lat, out[1].source_way_id) == far


def test_first_terminus_wins_a_cell():
    # Two dead-ends in one cell: the first replaces the sample; the second does
    # not clobber the first.
    sample = _cand(LON0, LAT0, way_id=10)
    t1 = (_east(30.0), LAT0, 1)
    t2 = (_east(60.0), LAT0, 2)
    assert grid_cell(*t1[:2], GRID_M) == grid_cell(*t2[:2], GRID_M) == grid_cell(LON0, LAT0, GRID_M)

    out = _inject_termini([sample], [t1, t2], GRID_M, max_candidates=5000)

    assert len(out) == 1
    assert out[0].source_way_id == 1  # first terminus kept


def test_replacement_happens_even_at_cap():
    # The cap blocks NEW cells, but replacing a sample in place must still work
    # (it doesn't grow the list).
    sample = _cand(LON0, LAT0, way_id=10)
    term = (_east(40.0), LAT0, 99)
    out = _inject_termini([sample], [term], GRID_M, max_candidates=1)
    assert len(out) == 1
    assert out[0].source_way_id == 99  # snapped despite being at the cap


def test_new_cell_terminus_blocked_at_cap():
    sample = _cand(LON0, LAT0, way_id=10)
    far = (_east(4000.0), LAT0, 7)
    out = _inject_termini([sample], [far], GRID_M, max_candidates=1)
    assert len(out) == 1  # cap reached -> the far terminus is not added
    assert out[0].source_way_id == 10
