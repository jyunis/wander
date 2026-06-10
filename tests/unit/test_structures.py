"""USA Structures ingest: occupancy filter + polygon WKT."""

from __future__ import annotations

from cairn.ingest.usa_structures import (
    _SKIP_OCC,
    STRUCT_ID_OFFSET,
    ring_to_polygon_wkt,
)


def test_polygon_wkt_from_rings():
    ring = [[-90.98, 45.92], [-90.97, 45.92], [-90.97, 45.93], [-90.98, 45.92]]
    wkt = ring_to_polygon_wkt([ring])
    assert wkt.startswith("POLYGON((")
    assert ring_to_polygon_wkt([]) is None
    assert ring_to_polygon_wkt([[[-90.98, 45.92]]]) is None  # too few points


def test_ring_is_closed_if_open():
    # An unclosed ring gets closed (first point appended).
    wkt = ring_to_polygon_wkt([[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]])
    assert wkt.endswith("0.0 0.0))")


def test_occupancy_skip_set():
    # Dwellings/commercial are avoided; sheds/toilets/barns are not.
    assert "Utility and Misc" in _SKIP_OCC
    assert "Agriculture" in _SKIP_OCC
    assert "Residential" not in _SKIP_OCC
    assert "Commercial" not in _SKIP_OCC


def test_id_offset_disjoint():
    assert STRUCT_ID_OFFSET > 10**15  # above OSM and MVUM id ranges
