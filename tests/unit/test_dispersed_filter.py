"""The `_is_dispersed` tag filter (scripts/load_overlays.py).

The dispersed layer's value hinges on this filter: keep established primitive
sites, drop developed/commercial/private ones that OSM under-tags. Loaded by
file path because scripts/ isn't an installed package (and the module's heavy
SHP deps are import-guarded so this works without pyshp/shapely).
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
_is_dispersed = load_overlays._is_dispersed


# --- kept: established primitive / dispersed sites -------------------------
def test_camp_site_basic_kept():
    assert _is_dispersed({"camp_site": "basic"}) is True


def test_backcountry_kept():
    assert _is_dispersed({"backcountry": "yes"}) is True


def test_informal_kept():
    assert _is_dispersed({"informal": "yes"}) is True


def test_bare_node_kept():
    # A plain tourism=camp_site node with no developed/commercial signals.
    assert _is_dispersed({}) is True


def test_basic_overrides_a_name_that_looks_developed():
    # Positive primitive signal wins even if the name contains a stop-word.
    assert _is_dispersed({"camp_site": "basic", "name": "Bear Camp Lodge"}) is True


# --- dropped: developed / commercial / private ----------------------------
def test_developed_camp_site_dropped():
    assert _is_dispersed({"camp_site": "standard"}) is False
    assert _is_dispersed({"camp_site": "pitch"}) is False


def test_fee_dropped():
    assert _is_dispersed({"fee": "yes"}) is False


def test_operator_dropped():
    assert _is_dispersed({"operator": "KOA"}) is False


def test_reservation_required_dropped():
    assert _is_dispersed({"reservation": "required"}) is False


def test_private_access_dropped():
    assert _is_dispersed({"access": "private"}) is False


def test_business_tag_dropped():
    # website/phone/address betray a developed site even with no operator/fee.
    assert _is_dispersed({"website": "https://example.com"}) is False
    assert _is_dispersed({"phone": "+1-555-0100"}) is False
    assert _is_dispersed({"addr:housenumber": "100"}) is False


def test_developed_name_dropped():
    for name in ("Canyon Lake Cottages", "Rafter J Bar Ranch", "French Creek CG",
                 "Pine Resort", "Big RV Park"):
        assert _is_dispersed({"name": name}) is False, name
