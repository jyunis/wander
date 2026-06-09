"""Overpass client: tag-filtered/versioned query + classification (FC-1, TLC-14)."""

from __future__ import annotations

from cairn.clients.overpass import (
    QUERY_VERSION,
    build_query,
    classify_osm,
)
from cairn.store.types import Bbox

_BBOX = Bbox(-91.88, 47.90, -91.86, 47.92)


def test_query_is_json_and_versioned_bbox_ordered():
    q = build_query(_BBOX)
    assert q.startswith("[out:json]")
    # Overpass bbox order is (south, west, north, east).
    assert "47.9,-91.88,47.92,-91.86" in q.replace("47.90", "47.9")
    assert q.rstrip().endswith("out geom;")
    assert QUERY_VERSION == 1


def test_query_only_requests_scored_classes_tlc14():
    q = build_query(_BBOX)
    for tag in ("highway", "natural", "waterway", "landuse", "place", "tourism"):
        assert tag in q
    # Nothing outside the scored vocabulary should leak into the query.
    for forbidden in ("amenity", "shop", "building", "leisure", "power", "barrier"):
        assert forbidden not in q


def test_classify_disambiguates_by_tag_key():
    assert classify_osm({"tags": {"highway": "track"}}) == "drivable"
    assert classify_osm({"tags": {"highway": "service"}}) == "drivable"
    assert classify_osm({"tags": {"highway": "primary"}}) == "highway"
    # residential is drivable as a highway but built-up as a landuse.
    assert classify_osm({"tags": {"highway": "residential"}}) == "drivable"
    assert classify_osm({"tags": {"landuse": "residential"}}) == "builtup"
    assert classify_osm({"tags": {"natural": "water"}}) == "water"
    assert classify_osm({"tags": {"waterway": "stream"}}) == "water"
    assert classify_osm({"tags": {"landuse": "forest"}}) == "forest"
    assert classify_osm({"tags": {"natural": "wood"}}) == "forest"
    assert classify_osm({"tags": {"place": "village"}}) == "place"
    assert classify_osm({"tags": {"tourism": "camp_site"}}) == "campsite"
    assert classify_osm({"tags": {"amenity": "bench"}}) is None
    assert classify_osm({}) is None
