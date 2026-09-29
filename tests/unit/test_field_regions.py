"""Offline-basemap region registry (display-only — TLC-3).

A region describes itself: the registry reads bbox/zooms/center out of the
PMTiles v3 header rather than a sidecar that could disagree with the bytes we
actually ship. These tests pin that parse, and pin the registry's refusal to be
taken anywhere outside `regions_dir`.
"""

from __future__ import annotations

import struct

import pytest

from cairn.field import (
    InvalidPMTiles,
    RegionStore,
    mvum_info,
    pin_feature,
    pins_feature_collection,
    read_header,
    road_feature,
    road_style,
    roads_feature_collection,
    valid_region_id,
)


def _header(
    *,
    bbox=(-92.9, 47.0, -89.9, 48.3),
    center=(-91.4, 47.65),
    min_zoom=0,
    max_zoom=14,
    magic=b"PMTiles",
    version=3,
) -> bytes:
    """A synthetic 127-byte PMTiles v3 header — the registry only ever reads
    these bytes, so a real 31 MB archive would be a pointless fixture."""
    h = bytearray(127)
    h[0:7] = magic
    h[7] = version
    h[100] = min_zoom
    h[101] = max_zoom
    struct.pack_into("<iiii", h, 102, *[round(v * 1e7) for v in bbox])
    h[118] = 8  # center zoom
    struct.pack_into("<ii", h, 119, *[round(v * 1e7) for v in center])
    return bytes(h)


def _write(dirpath, region_id: str, **kw) -> None:
    # Trailing bytes stand in for the tile data; nothing reads past the header.
    (dirpath / f"{region_id}.pmtiles").write_bytes(_header(**kw) + b"\0" * 64)


def test_read_header_parses_bbox_zooms_and_center(tmp_path):
    _write(tmp_path, "superior-nf")
    head = read_header(tmp_path / "superior-nf.pmtiles")

    assert head["bbox"] == pytest.approx((-92.9, 47.0, -89.9, 48.3))
    assert head["center"] == pytest.approx((-91.4, 47.65))
    assert (head["min_zoom"], head["max_zoom"]) == (0, 14)


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"magic": b"NOTPMT!"}, "bad magic"),
        ({"version": 2}, "unsupported version"),
    ],
)
def test_read_header_rejects_non_pmtiles_v3(tmp_path, kw, reason):
    _write(tmp_path, "bogus", **kw)
    with pytest.raises(InvalidPMTiles):
        read_header(tmp_path / "bogus.pmtiles")


def test_read_header_rejects_truncated_file(tmp_path):
    (tmp_path / "short.pmtiles").write_bytes(b"PMTiles\x03")
    with pytest.raises(InvalidPMTiles):
        read_header(tmp_path / "short.pmtiles")


def test_list_names_known_regions_and_titleizes_the_rest(tmp_path):
    _write(tmp_path, "superior-nf")
    _write(tmp_path, "some-new-forest")

    regions = {r.id: r for r in RegionStore(tmp_path).list()}

    assert regions["superior-nf"].name == "Superior National Forest"
    # Unlisted id still shows up — building a region shouldn't need a code change.
    assert regions["some-new-forest"].name == "Some New Forest"


def test_list_reports_size_and_sorts_by_name(tmp_path):
    _write(tmp_path, "superior-nf")
    _write(tmp_path, "chippewa-nf")

    regions = RegionStore(tmp_path).list()

    assert [r.id for r in regions] == ["chippewa-nf", "superior-nf"]  # by display name
    assert regions[0].size_bytes == 127 + 64
    assert regions[0].built_at > 0


def test_list_skips_corrupt_archives_rather_than_failing(tmp_path):
    """A half-finished download shouldn't take the whole field view down."""
    _write(tmp_path, "superior-nf")
    (tmp_path / "truncated.pmtiles").write_bytes(b"PMTiles\x03")
    (tmp_path / "garbage.pmtiles").write_bytes(b"not a pmtiles archive at all")

    assert [r.id for r in RegionStore(tmp_path).list()] == ["superior-nf"]


def test_store_is_inert_without_a_regions_dir(tmp_path):
    """Cairn runs fine with no region built — the field view just offers none."""
    store = RegionStore(tmp_path / "nope")
    assert store.list() == []
    assert store.get("superior-nf") is None
    assert store.path_for("superior-nf") is None


def test_get_returns_none_for_unknown_region(tmp_path):
    assert RegionStore(tmp_path).get("superior-nf") is None


@pytest.mark.parametrize(
    "region_id",
    ["../../etc/passwd", "..", "a/b", "sup.nf", "Superior", "", "-lead", "x" * 65],
)
def test_region_ids_that_could_escape_the_dir_are_rejected(region_id):
    assert not valid_region_id(region_id)


def test_traversal_id_cannot_reach_a_file_outside_regions_dir(tmp_path):
    """`get`/`path_for` take their id straight from the URL path."""
    outside = tmp_path / "secret.pmtiles"
    outside.write_bytes(_header())
    regions = tmp_path / "regions"
    regions.mkdir()

    store = RegionStore(regions)
    assert store.get("../secret") is None
    assert store.path_for("../secret") is None


def test_as_dict_is_the_wire_shape(tmp_path):
    _write(tmp_path, "superior-nf")
    payload = RegionStore(tmp_path).get("superior-nf").as_dict()

    assert payload["id"] == "superior-nf"
    assert payload["url"] == "/field/regions/superior-nf.pmtiles"
    assert payload["bbox"] == pytest.approx([-92.9, 47.0, -89.9, 48.3])
    assert payload["max_zoom"] == 14
    assert payload["size_bytes"] == 191
    # Pins live beside the archive, so the URL is always advertised; whether it
    # has any features is the endpoint's business (needs a cairn.db read the
    # registry doesn't do), so it can't be gated on a file check like outline.
    assert payload["pins_url"] == "/field/regions/superior-nf/pins.geojson"
    # Same for roads: whether any were ingested is a cairn.db question.
    assert payload["roads_url"] == "/field/regions/superior-nf/roads.geojson"


# --- Wander content on the offline map (#45 W1) ------------------------------


def test_pin_feature_surfaces_a_scored_site_score(tmp_path):
    """A scored site's saved payload carries its absolute total; the pin lifts it
    to `score` so the client can color it on the same ramp the online app uses,
    while keeping the whole payload inline for the offline card."""
    place = {
        "id": 7, "kind": "site", "name": "Near Mallard Lake",
        "lat": 47.65, "lon": -91.4, "rating": 4, "note": "quiet",
        "payload": {"total": 0.72, "rank": 3, "contributions": [{"feature": "road_score"}]},
    }
    feat = pin_feature(place)

    assert feat["geometry"] == {"type": "Point", "coordinates": [-91.4, 47.65]}
    props = feat["properties"]
    assert props["kind"] == "site"
    assert props["name"] == "Near Mallard Lake"
    assert props["score"] == pytest.approx(0.72)
    assert props["rating"] == 4
    # The full payload rides along so the card reads score/contributions offline.
    assert props["payload"]["contributions"] == [{"feature": "road_score"}]


def test_pin_feature_has_no_score_for_a_scoreless_place():
    """Campgrounds and hand-dropped pins have no total — no `score` key at all,
    so the client falls back to a by-kind color instead of the ramp."""
    for place in (
        {"id": 1, "kind": "campground", "name": "State CG", "lat": 47.5, "lon": -91.0,
         "rating": None, "note": None, "payload": {"url": "https://example/book"}},
        {"id": 2, "kind": "site", "name": "Dropped", "lat": 47.5, "lon": -91.0,
         "rating": None, "note": None, "payload": None},
    ):
        assert "score" not in pin_feature(place)["properties"]


def test_pins_feature_collection_is_a_featurecollection():
    fc = pins_feature_collection([
        {"id": 1, "kind": "site", "name": "A", "lat": 47.5, "lon": -91.0,
         "rating": None, "note": None, "payload": {"total": 0.6}},
    ])
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    assert pins_feature_collection([]) == {"type": "FeatureCollection", "features": []}


# --- Roads on the offline map ------------------------------------------------


def _road(coords=((-91.4, 47.6), (-91.39, 47.61)), **props):
    base = {
        "osm_id": 7, "highway": "track", "name": None, "ref": None,
        "forest_road": False, "rough": False, "mvum_access": None,
        "access": None, "private": False, "surface": None, "mud_tier": None,
    }
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
        "properties": {**base, **props},
    }


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        ("Roads open to all Vehicles, Seasonal",
         {"class": "Open to all vehicles", "season": "Seasonal", "special": False}),
        ("Roads open to Highway Legal Vehicles only, Yearlong",
         {"class": "Highway-legal vehicles only", "season": "Yearlong", "special": False}),
        ("Special Designation", {"class": "Special designation", "season": None, "special": True}),
        ("Something new", {"class": "Something new", "season": None, "special": False}),
    ],
)
def test_mvum_info_reads_class_and_season(access, expected):
    assert mvum_info(access) == expected


def test_mvum_info_is_none_off_the_mvum():
    assert mvum_info(None) is None
    assert mvum_info("") is None


def test_road_style_precedence():
    """Private wins even on a forest road: that's the one you may not drive."""
    assert road_style({"private": True, "forest_road": True}) == "private"
    assert road_style({"forest_road": True, "mvum_access": "Special Designation"}) == "special"
    assert road_style({"forest_road": True}) == "forest"
    assert road_style({}) == "road"


def test_road_feature_names_a_forest_road_by_its_number_and_reads_the_mvum():
    props = road_feature(_road(
        ref="203", name="203", forest_road=True, surface="gravel", mud_tier="Soft",
        mvum_access="Roads open to all Vehicles, Seasonal",
    ))["properties"]

    assert props["label"] == "FR 203"
    assert "name" not in props  # the name is just the ref again
    # ...or just the label again (MVUM names are often "FR 203" themselves).
    assert "name" not in road_feature(_road(ref="203", name="FR 203"))["properties"]
    named = road_feature(_road(ref="203", name="Moose Lake Rd"))["properties"]
    assert named["name"] == "Moose Lake Rd"
    assert props["style"] == "forest"
    assert props["mvum_class"] == "Open to all vehicles"
    assert props["season"] == "Seasonal"
    assert props["surface"] == "gravel"
    assert props["mud_tier"] == "Soft"


def test_road_feature_drops_empty_properties():
    """Most properties are null on most roads; the sidecar is a phone download."""
    props = road_feature(_road())["properties"]
    assert props == {"osm_id": 7, "style": "road", "label": "Road", "highway": "track"}


def test_road_feature_keeps_access_only_when_it_says_more_than_private():
    assert "access" not in road_feature(_road(private=True, access="private"))["properties"]
    props = road_feature(_road(private=True, access="permit"))["properties"]
    assert props["private"] is True
    assert props["access"] == "permit"
    assert props["style"] == "private"


def test_road_feature_rounds_coordinates_and_drops_repeats():
    feat = road_feature(_road(coords=(
        (-91.4000001, 47.6000001), (-91.4000002, 47.6000002), (-91.3912345678, 47.6123456789),
    )))
    assert feat["geometry"]["coordinates"] == [[-91.4, 47.6], [-91.39123, 47.61235]]


def test_road_feature_that_collapses_to_a_point_is_dropped():
    assert road_feature(_road(coords=((-91.4, 47.6), (-91.4000001, 47.6000001)))) is None


def test_roads_feature_collection_carries_truncation():
    fc = roads_feature_collection({"truncated": True, "features": [_road()]})
    assert fc["type"] == "FeatureCollection"
    assert fc["truncated"] is True
    assert len(fc["features"]) == 1
    assert roads_feature_collection({"features": []}) == {
        "type": "FeatureCollection", "truncated": False, "features": [],
    }
