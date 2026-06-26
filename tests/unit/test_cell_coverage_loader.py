"""The pure helpers behind the `cell_coverage` overlay loader
(scripts/load_overlays.py): carrier-brand normalization, source-file bucketing,
field detection, res-9 validation, the coverage-quality grade, and hexagon WKT.

Loaded by file path because scripts/ isn't an installed package (deps are
import-guarded). The hex helpers need `h3` (in the dev extra); they're skipped
if it's somehow absent.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "load_overlays",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "load_overlays.py"),
)
load_overlays = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(load_overlays)

_carrier = load_overlays._cell_carrier
_source = load_overlays._cell_source
_find_field = load_overlays._find_field
_res9 = load_overlays._res9_cell
_tier = load_overlays._coverage_tier
_classify = load_overlays._classify_parents
_poly = load_overlays._hex_polygon_wkt
h3 = load_overlays.h3
RES = load_overlays.CELL_HEX_RES
SRC = load_overlays.CELL_SRC_RES
SOLID_MIN = load_overlays._CELL_SOLID_MIN
FAST_MIN = load_overlays._CELL_FAST_MIN
CHILDREN = load_overlays._CELL_CHILDREN

needs_h3 = pytest.mark.skipif(h3 is None, reason="h3 not installed")


# --- carrier normalization -------------------------------------------------
def test_carrier_majors():
    assert _carrier("Verizon") == "Verizon"
    assert _carrier("Verizon Wireless") == "Verizon"
    assert _carrier("AT&T") == "AT&T"
    assert _carrier("AT&T Mobility") == "AT&T"
    assert _carrier("New Cingular Wireless PCS, LLC") == "AT&T"
    assert _carrier("T-Mobile") == "T-Mobile"
    assert _carrier("T-Mobile US, Inc.") == "T-Mobile"
    assert _carrier("U.S. Cellular") == "UScellular"
    assert _carrier("UScellular") == "UScellular"


def test_carrier_regional_and_empty_dropped():
    assert _carrier("Some Regional Telephone Co-op") is None
    assert _carrier("") is None
    assert _carrier(None) is None


def test_source_bucket_from_filename():
    # Coverage bucket derived from the BDC filename.
    assert _source("bdc_27_130403_4GLTE_mobile_broadband_h3_D25.gpkg") == "any"
    assert _source("bdc_27_130403_5GNR_7_1_mobile_broadband_h3_D25.gpkg") == "any"
    assert _source("bdc_27_5GNR_mobile_broadband_h3_D25.gpkg") == "any"  # tier-less 5G
    # Only the 35/3 5G file feeds the "fast" tally.
    assert _source("bdc_27_130403_5GNR_35_3_mobile_broadband_h3_D25.gpkg") == "fast"
    assert _source("bdc_27_130403_3G_mobile_broadband_h3.gpkg") is None  # 3G skipped


def test_carrier_from_provider_filename():
    # "By Provider" single-carrier files carry the carrier in the filename, not a
    # column — the loader falls back to _cell_carrier(basename).
    assert _carrier("bdc_27_TMobile_5GNR_mobile_broadband_h3_D25.gpkg") == "T-Mobile"
    assert _carrier("bdc_27_Verizon_4GLTE_mobile_broadband_h3.gpkg") == "Verizon"
    # An all-provider aggregate filename has no carrier token -> None (dropped).
    assert _carrier("bdc_27_5GNR_mobile_broadband_h3_D25_23jun2026.gpkg") is None


# --- field detection -------------------------------------------------------
def test_find_field():
    flds = ["provider_id", "brand_name", "technology", "h3_res9_id"]
    assert _find_field(flds, "brand", "holding") == 1
    assert _find_field(flds, "h3", "hex") == 3
    assert _find_field(flds, "geom", "shape") is None


# --- hex validation + quality grading --------------------------------------
@needs_h3
def test_res9_cell_accepts_valid_rejects_garbage():
    cell9 = h3.latlng_to_cell(46.5, -92.5, SRC)
    assert _res9(cell9) == cell9
    assert _res9("not-a-hex") is None
    assert _res9("") is None
    # A cell coarser than res-7 has no res-7 parent to count toward -> rejected.
    assert _res9(h3.latlng_to_cell(46.5, -92.5, 5)) is None


def test_coverage_tier_grades():
    # Fast bar = 25/49 (>= 50% fast 5G); Solid bar = 33/49 (>= 2/3 any coverage).
    assert FAST_MIN == 25 and SOLID_MIN == 33 and CHILDREN == 49
    assert _tier(any_count=49, fast_count=49) == "Fast"
    assert _tier(any_count=49, fast_count=25) == "Fast"
    assert _tier(any_count=49, fast_count=24) == "Solid"  # not enough fast, but well covered
    assert _tier(any_count=33, fast_count=0) == "Solid"
    assert _tier(any_count=32, fast_count=0) == "Patchy"  # just under the 2/3 Solid bar
    # The Whitewater case: ~57% covered (28/49) now reads Patchy, not Solid.
    assert _tier(any_count=28, fast_count=5) == "Patchy"
    assert _tier(any_count=1, fast_count=0) == "Patchy"
    assert _tier(any_count=0, fast_count=0) is None


@needs_h3
def test_classify_parents_assigns_best_tier_per_hex():
    parent = h3.latlng_to_cell(46.5, -92.5, RES)
    kids = list(h3.cell_to_children(parent, SRC))
    assert len(kids) == CHILDREN
    # 26 covered, 25 of them fast -> Fast.
    assert _classify(set(kids[:26]), set(kids[:25])) == {parent: "Fast"}
    # 35 covered (>= 2/3), only 10 fast -> Solid (well-covered but not fast).
    assert _classify(set(kids[:35]), set(kids[:10])) == {parent: "Solid"}
    # 28 covered, below the 2/3 bar -> Patchy (the Whitewater band).
    assert _classify(set(kids[:28]), set(kids[:5])) == {parent: "Patchy"}
    # No covered children -> not in the map (a real gap).
    assert _classify(set(), set()) == {}


@needs_h3
def test_hex_polygon_wkt_is_closed_lonlat_ring():
    parent = h3.latlng_to_cell(46.5, -92.5, RES)
    wkt = _poly(parent)
    assert wkt.startswith("POLYGON((") and wkt.endswith("))")
    coords = [
        tuple(map(float, pair.split()))
        for pair in wkt[len("POLYGON((") : -2].split(",")
    ]
    assert coords[0] == coords[-1]  # closed
    assert len(coords) >= 7  # hexagon ring (6 verts + closure)
    lon, lat = coords[0]
    assert -180 <= lon <= 180 and -90 <= lat <= 90  # lon/lat order, in MN range
    assert -95 < lon < -90 and 45 < lat < 48
