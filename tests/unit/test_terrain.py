"""Terrain client: tile math, decode roundtrip, slope, and the TLC-13 bound."""

from __future__ import annotations

import numpy as np
import pytest

from cairn.clients.terrain import (
    TILE_PX,
    TerrainSampler,
    _TileLRU,
    decode_terrarium,
    lonlat_to_tile,
    meters_per_pixel,
    slope_at_pixel,
    tile_covering,
)
from cairn.config import Settings
from cairn.store.types import Bbox
from tests.fakes.terrarium import encode_terrarium, flat_tile, ramp_tile_x


def test_lonlat_to_tile_known_value():
    # Ely, MN at z12 -> tile (1002, 1425) (confirmed against the live bucket).
    x, y = lonlat_to_tile(-91.86, 47.90, 12)
    assert (int(x), int(y)) == (1002, 1425)


def test_decode_roundtrip():
    elev = np.linspace(-200, 4000, TILE_PX * TILE_PX, dtype=np.float32).reshape(
        TILE_PX, TILE_PX
    )
    decoded = decode_terrarium(encode_terrarium(elev))
    assert np.allclose(decoded, elev, atol=0.02)  # ~1/256 m quantization


def test_slope_flat_is_zero():
    elev = flat_tile(250.0)
    assert slope_at_pixel(elev, 128.0, 128.0, 47.9, 12) == pytest.approx(0.0, abs=1e-6)


def test_slope_matches_known_gradient():
    res = meters_per_pixel(47.9, 12)
    elev = ramp_tile_x(meters_per_col=res)  # rise == run -> 45 degrees
    got = slope_at_pixel(elev, 128.0, 128.0, 47.9, 12)
    assert got == pytest.approx(45.0, abs=0.5)


def test_tile_covering_is_inclusive_range():
    cov = tile_covering(Bbox(-91.90, 47.88, -91.82, 47.94), 12)
    assert cov["zoom"] == 12
    assert cov["x_min"] <= 1002 <= cov["x_max"]
    assert cov["y_min"] <= 1425 <= cov["y_max"]


def test_tile_lru_never_exceeds_cap():
    lru = _TileLRU(cap=4)
    for i in range(50):
        lru.put((12, i, 0), np.zeros((4, 4), dtype=np.float32))
    assert len(lru) <= 4
    assert lru.max_resident <= 4


async def test_sampler_resident_tiles_bounded_across_many(tmp_path):
    """TLC-13: sampling points spanning N >> cap tiles keeps resident decoded
    tiles <= cap. Tiles are pre-seeded into the disk cache so no network runs."""
    settings = Settings(_env_file=None, data_dir=tmp_path, tile_lru_cap=8)
    sampler = TerrainSampler(settings, zoom=12)

    # Seed 40 distinct tiles directly into the disk cache.
    png = encode_terrarium(flat_tile(300.0))
    base_x, base_y = 1002, 1425
    for k in range(40):
        sampler._cache.put(f"terrarium/12/{base_x + k}/{base_y}", png)

    # Sample one point inside each of those 40 tiles.
    n = 2**12
    for k in range(40):
        tx = base_x + k
        # lon at the western edge of tile tx, mid-latitude of tile base_y.
        lon = tx / n * 360.0 - 180.0 + 0.01
        lat = 47.90
        slope = await sampler.slope_deg(lon, lat)
        assert slope == pytest.approx(0.0, abs=1e-6)

    assert len(sampler.tiles_used) >= 30  # spanned many distinct tiles
    assert sampler.max_resident_tiles <= 8  # never more than the cap resident
    await sampler.aclose()
