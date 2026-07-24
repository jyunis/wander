"""Region builder: size estimation, naming, and the guardrails on a drawn box.

The estimate and the cap are counted in TILES, not degrees². Web Mercator
stretches high latitudes, so an identical-looking box holds ~27% more map in the
BWCA than in south Texas — and a cap in degrees² would wave through a northern
box that costs far more than a southern one of the same size.
"""

from __future__ import annotations

import pytest

from cairn.config import Settings
from cairn.errors import ValidationError
from cairn.regionbuild import count_tiles, slugify, validate_request

# The one real extract we have to calibrate against.
SUPERIOR_BBOX = (-92.9, 47.0, -89.9, 48.3)
SUPERIOR_TILES = 16_443
SUPERIOR_BYTES = 31_880_290


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path)


def test_tile_count_matches_the_real_archive():
    """If this drifts, every size estimate the user sees is wrong."""
    assert count_tiles(SUPERIOR_BBOX, 14) == SUPERIOR_TILES


def test_no_bytes_per_tile_constant_can_serve_two_boxes():
    """Why sizing shells out to `pmtiles --dry-run` instead of multiplying.

    Superior NF: 16,443 tiles / 31,880,290 B = 1939 B/tile.
    A BWCA trip box: 331 tiles / 2,194,362 B = 6629 B/tile — 3.4x denser, because
    a small box is dominated by fixed low-zoom overhead (a z6 tile is ~63 kB, a
    z14 tile ~1.1 kB) and by how much shoreline is in it.
    """
    assert SUPERIOR_BYTES / count_tiles(SUPERIOR_BBOX, 14) == pytest.approx(1939, abs=1)
    bwca_bytes, bwca_tiles = 2_194_362, count_tiles((-91.9, 47.85, -91.6, 48.05), 14)
    assert bwca_bytes / bwca_tiles == pytest.approx(6629, abs=1)


def test_latitude_changes_the_tile_count_of_an_identical_box():
    """Same degrees², different amount of map — which is why the cap counts tiles.

    Mercator stretches as you go north, so the northern box costs MORE tiles. A
    cap expressed in degrees² would silently under-charge the BWCA.
    """
    north = count_tiles((-92.9, 47.0, -89.9, 48.3), 12)  # BWCA
    south = count_tiles((-92.9, 25.0, -89.9, 26.3), 12)  # south Texas
    assert north > south
    assert north / south == pytest.approx(1.27, abs=0.05)


def test_a_trip_sized_box_is_tiny():
    """The point of letting the user draw: a real trip area is a rounding error
    next to a forest-sized rectangle. 331 tiles vs 16,443."""
    trip = count_tiles((-91.9, 47.85, -91.6, 48.05), 14)
    assert trip == 331
    assert trip < count_tiles(SUPERIOR_BBOX, 14) / 40


def test_zooms_are_cumulative():
    """An archive carries the whole pyramid z0..maxzoom, not just the top."""
    assert count_tiles(SUPERIOR_BBOX, 14) > count_tiles(SUPERIOR_BBOX, 13)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("BWCA trip", "bwca-trip"),
        ("Labor Day 2026!", "labor-day-2026"),
        ("  Kawishiwi  ", "kawishiwi"),
        ("Bob's Camp", "bob-s-camp"),
        ("Sawtooth — North", "sawtooth-north"),
    ],
)
def test_slugify_makes_a_url_safe_id(name, expected):
    assert slugify(name) == expected


def test_validate_accepts_a_reasonable_box(settings):
    region_id, tiles = validate_request("BWCA trip", [-91.9, 47.85, -91.6, 48.05], 14, settings)
    assert region_id == "bwca-trip"
    assert tiles == 331


def test_validate_rejects_a_box_bigger_than_the_cap(settings):
    with pytest.raises(ValidationError, match="too big"):
        validate_request("Half of Canada", [-120.0, 45.0, -80.0, 60.0], 14, settings)


def test_validate_rejects_an_inverted_box(settings):
    with pytest.raises(ValidationError, match="min < max"):
        validate_request("Backwards", [-89.9, 48.3, -92.9, 47.0], 14, settings)


def test_validate_rejects_a_box_off_the_map(settings):
    with pytest.raises(ValidationError, match="outside the mapped world"):
        validate_request("North Pole", [-10.0, 86.0, 10.0, 89.0], 14, settings)


def test_validate_rejects_zoom_outside_the_supported_range(settings):
    with pytest.raises(ValidationError, match="maxzoom"):
        validate_request("Too detailed", [-91.9, 47.85, -91.6, 48.05], 18, settings)


@pytest.mark.parametrize("name", ["", "   ", "!!!", "🙂"])
def test_validate_rejects_names_with_no_usable_id(settings, name):
    with pytest.raises(ValidationError):
        validate_request(name, [-91.9, 47.85, -91.6, 48.05], 14, settings)


def test_a_lower_maxzoom_is_the_escape_hatch_for_a_big_box(settings):
    """The pre-filter counts tiles, so dropping detail is how you fit a larger
    area: most of Minnesota is 129,646 tiles at z14 but only 8,366 at z12."""
    big = [-95.0, 45.0, -88.0, 49.5]
    assert count_tiles(big, 14) > settings.region_max_tiles
    with pytest.raises(ValidationError, match="too big"):
        validate_request("Big at z14", big, 14, settings)

    assert count_tiles(big, 12) < settings.region_max_tiles
    region_id, _ = validate_request("Big at z12", big, 12, settings)
    assert region_id == "big-at-z12"
