"""Offline-basemap region registry (display-only — TLC-3).

A region describes itself: the registry reads bbox/zooms/center out of the
PMTiles v3 header rather than a sidecar that could disagree with the bytes we
actually ship. These tests pin that parse, and pin the registry's refusal to be
taken anywhere outside `regions_dir`.
"""

from __future__ import annotations

import struct

import pytest

from cairn.field import InvalidPMTiles, RegionStore, read_header, valid_region_id


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
