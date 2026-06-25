"""The pure helpers behind the resupply `amenity` overlay loader
(scripts/load_overlays.py): categorize OSM tags into resupply/town categories,
name a row, and merge buffered forest bboxes into query regions.

Loaded by file path because scripts/ isn't an installed package (and the SHP
deps are import-guarded so this works without pyshp/shapely).
"""

from __future__ import annotations

import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "load_overlays",
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "load_overlays.py"),
)
load_overlays = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(load_overlays)
_categorize = load_overlays._categorize_amenity
_name = load_overlays._amenity_name
_merge = load_overlays._merge_bboxes
_meta = load_overlays._amenity_meta


# --- categorization --------------------------------------------------------
def test_fuel():
    assert _categorize({"amenity": "fuel"}) == {"fuel"}


def test_groceries_variants():
    assert _categorize({"shop": "supermarket"}) == {"groceries"}
    assert _categorize({"shop": "convenience"}) == {"groceries"}
    assert _categorize({"shop": "boutique"}) == set()  # not a grocery


def test_drinking_water():
    assert _categorize({"amenity": "drinking_water"}) == {"water"}
    assert _categorize({"man_made": "water_tap", "drinking_water": "yes"}) == {"water"}
    assert _categorize({"man_made": "water_tap"}) == set()  # tap, not potable


def test_dump_station_variants():
    assert _categorize({"amenity": "sanitary_dump_station"}) == {"dump_station"}
    assert _categorize({"waste_disposal": "dump_station"}) == {"dump_station"}


def test_propane_variants():
    assert _categorize({"shop": "gas"}) == {"propane"}
    assert _categorize({"amenity": "fuel", "fuel:propane": "yes"}) == {"fuel", "propane"}
    assert _categorize({"amenity": "fuel", "fuel:lpg": "yes"}) == {"fuel", "propane"}


def test_town_places():
    assert _categorize({"place": "town"}) == {"town"}
    assert _categorize({"place": "city"}) == {"town"}
    assert _categorize({"place": "hamlet"}) == set()  # too small to be a resupply town


def test_multi_category_element():
    # A fuel station + convenience store that also sells propane → all three.
    cats = _categorize({"amenity": "fuel", "shop": "convenience", "fuel:propane": "yes"})
    assert cats == {"fuel", "groceries", "propane"}


def test_untagged_is_nothing():
    assert _categorize({}) == set()


# --- naming ----------------------------------------------------------------
def test_name_prefers_osm_name():
    assert _name("fuel", {"name": "Holiday"}) == "Holiday"


def test_name_falls_back_per_category():
    assert _name("fuel", {}) == "Gas station"
    assert _name("dump_station", {}) == "Dump station"


def test_unnamed_town_is_dropped():
    assert _name("town", {}) is None
    assert _name("town", {"name": "Walker"}) == "Walker"


# --- meta ------------------------------------------------------------------
def test_meta_is_compact():
    assert _meta("fuel", {"brand": "Holiday"}) == {"brand": "Holiday"}
    assert _meta("fuel", {}) == {}  # nothing to show
    assert _meta("town", {"place": "town", "population": "1200"}) == {
        "place": "town", "population": "1200"
    }


# --- bbox merge ------------------------------------------------------------
def test_merge_disjoint_unchanged():
    boxes = [(0, 0, 1, 1), (5, 5, 6, 6)]
    assert sorted(_merge(boxes)) == sorted(boxes)


def test_merge_overlapping_unions():
    # Two overlapping boxes collapse into their bounding union.
    merged = _merge([(0, 0, 2, 2), (1, 1, 3, 3)])
    assert merged == [(0, 0, 3, 3)]


def test_merge_chain_transitively():
    # A overlaps B, B overlaps C, A and C don't directly — all three still merge.
    merged = _merge([(0, 0, 2, 2), (1, 1, 3, 3), (2, 2, 4, 4)])
    assert merged == [(0, 0, 4, 4)]
