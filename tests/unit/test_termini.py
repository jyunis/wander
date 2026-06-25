"""Dead-end (terminus) detection on drivable ways (FC-3)."""

from __future__ import annotations

from cairn.engine.candidates import find_termini, grid_cell
from cairn.engine.types import Way
from cairn.store.types import Bbox

BBOX = Bbox(-0.05, -0.05, 0.05, 0.05)


def test_isolated_way_has_two_termini():
    # A lone way: both endpoints are dead-ends.
    w = Way(osm_id=1, coords=((-0.01, 0.0), (0.01, 0.0)))
    t = find_termini([w], BBOX)
    assert len(t) == 2
    assert {(round(x, 3), round(y, 3)) for x, y, _ in t} == {(-0.01, 0.0), (0.01, 0.0)}


def test_shared_junction_is_not_a_terminus():
    # Two ways meeting at (0,0): that shared end is a junction, the far ends
    # are dead-ends. -> 2 termini, not 4.
    a = Way(osm_id=1, coords=((-0.02, 0.0), (0.0, 0.0)))
    b = Way(osm_id=2, coords=((0.0, 0.0), (0.0, 0.02)))
    t = find_termini([a, b], BBOX)
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in t}
    assert (0.0, 0.0) not in ends
    assert ends == {(-0.02, 0.0), (0.0, 0.02)}


def test_terminus_outside_bbox_excluded():
    # A way whose dead-end lies outside the bbox contributes no terminus there.
    w = Way(osm_id=1, coords=((0.0, 0.0), (0.2, 0.0)))  # far end outside
    t = find_termini([w], Bbox(-0.05, -0.05, 0.05, 0.05))
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in t}
    assert (0.0, 0.0) in ends  # in-bbox end kept
    assert all(-0.05 <= x <= 0.05 for x, _, _ in t)  # the 0.2 end dropped


def test_duplicate_geometry_does_not_mask_a_dead_end():
    # The real bug: one physical spur stored as three identical copies (the MVUM
    # service emits duplicate records with different ids). The shared free end is
    # still a dead-end — duplicates leave the vertex in the same direction, so
    # they must NOT register as "another road connects here".
    coords = ((-0.01, 0.0), (0.0, 0.0), (0.01, 0.0))
    ways = [Way(osm_id=i, coords=coords) for i in (1, 2, 3)]
    t = find_termini(ways, BBOX)
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in t}
    assert ends == {(-0.01, 0.0), (0.01, 0.0)}  # both real ends survive


def test_reversed_duplicate_also_does_not_mask():
    # A duplicate digitized in the opposite vertex order is still a duplicate
    # (same direction out of the shared end), not a junction.
    a = Way(osm_id=1, coords=((-0.01, 0.0), (0.01, 0.0)))
    b = Way(osm_id=2, coords=((0.01, 0.0), (-0.01, 0.0)))  # reversed
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in find_termini([a, b], BBOX)}
    assert ends == {(-0.01, 0.0), (0.01, 0.0)}


def test_collinear_continuation_is_not_a_terminus():
    # A road split into two ways that continue straight through the shared
    # vertex: that vertex is NOT a dead-end (the road keeps going). The two
    # leave in OPPOSITE directions, so it's correctly a junction.
    a = Way(osm_id=1, coords=((-0.02, 0.0), (0.0, 0.0)))
    b = Way(osm_id=2, coords=((0.0, 0.0), (0.02, 0.0)))  # continues east
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in find_termini([a, b], BBOX)}
    assert (0.0, 0.0) not in ends
    assert ends == {(-0.02, 0.0), (0.02, 0.0)}


def test_through_road_makes_endpoint_a_junction():
    # A spur ending on a road that PASSES THROUGH the vertex is a junction.
    spur = Way(osm_id=1, coords=((0.0, 0.01), (0.0, 0.0)))
    thru = Way(osm_id=2, coords=((-0.02, 0.0), (0.0, 0.0), (0.02, 0.0)))
    ends = {(round(x, 3), round(y, 3)) for x, y, _ in find_termini([spur, thru], BBOX)}
    assert (0.0, 0.0) not in ends  # spur's lower end connects to the through road


def test_grid_cell_is_stable():
    assert grid_cell(-91.86, 47.9, 100.0) == grid_cell(-91.8600, 47.9000, 100.0)
