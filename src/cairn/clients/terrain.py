"""Terrarium terrain client + bounded slope sampler (FC-2, TLC-13).

AWS Terrarium tiles encode elevation as RGB PNGs:
    elev_m = (R * 256 + G + B / 256) - 32768

Cairn samples slope at sparse, road-anchored candidate points — it NEVER
materializes a full-bbox elevation or slope raster (TLC-13). Decoded float32
tiles pass through a bounded LRU (default <=16 tiles ~= 4 MB); slope at a point
is computed from a 1-px central-difference halo within its tile, scaled by the
per-pixel ground size at that latitude. Peak resident tile memory is
O(LRU_cap), independent of bbox area.

Decode and slope are pure functions of tile bytes; only tile fetch does I/O.
"""

from __future__ import annotations

import io
import logging
import math
from collections import OrderedDict

import httpx
import numpy as np
from PIL import Image

from cairn.clients.cache import DiskCache
from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import TileDecodeError, TileUnreachable
from cairn.store.types import Bbox

log = logging.getLogger("cairn.terrain")

TILE_PX = 256
# Web-mercator ground resolution at the equator, meters per pixel at z0.
_EQUATOR_M_PER_PX_Z0 = 2 * math.pi * 6378137.0 / TILE_PX  # ~156543.03


# --- pure tile math --------------------------------------------------------


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional (x, y) tile coordinates (Slippy/XYZ scheme)."""
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_covering(bbox: Bbox, zoom: int) -> dict:
    """Tile-set identity for a bbox: zoom + inclusive x/y ranges (provenance)."""
    b = bbox.normalized()
    x0, y0 = lonlat_to_tile(b.min_lon, b.max_lat, zoom)  # NW corner
    x1, y1 = lonlat_to_tile(b.max_lon, b.min_lat, zoom)  # SE corner
    return {
        "zoom": zoom,
        "x_min": int(math.floor(x0)),
        "x_max": int(math.floor(x1)),
        "y_min": int(math.floor(y0)),
        "y_max": int(math.floor(y1)),
    }


def meters_per_pixel(lat: float, zoom: int) -> float:
    return _EQUATOR_M_PER_PX_Z0 * math.cos(math.radians(lat)) / (2.0**zoom)


# --- pure decode + slope ---------------------------------------------------


def decode_terrarium(png_bytes: bytes) -> np.ndarray:
    """Decode a Terrarium PNG to a (TILE_PX, TILE_PX) float32 elevation array."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    except Exception as exc:  # PIL raises a grab-bag; wrap it (TLC-10)
        raise TileDecodeError(f"could not decode tile PNG: {exc}") from exc
    if rgb.shape[:2] != (TILE_PX, TILE_PX):
        raise TileDecodeError(f"unexpected tile shape {rgb.shape}")
    elev = rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0
    return elev.astype(np.float32)


def slope_at_pixel(elev: np.ndarray, px: float, py: float, lat: float, zoom: int) -> float:
    """Slope in degrees at fractional pixel (px=col, py=row) of a decoded tile.

    Central difference over a 1-px halo, clamped at tile edges, scaled by the
    ground size of a pixel at this latitude. Pure.
    """
    col = min(max(int(round(px)), 0), TILE_PX - 1)
    row = min(max(int(round(py)), 0), TILE_PX - 1)
    res_m = meters_per_pixel(lat, zoom)

    c0, c1 = max(col - 1, 0), min(col + 1, TILE_PX - 1)
    r0, r1 = max(row - 1, 0), min(row + 1, TILE_PX - 1)
    dz_dx = (elev[row, c1] - elev[row, c0]) / max(c1 - c0, 1) / res_m
    dz_dy = (elev[r1, col] - elev[r0, col]) / max(r1 - r0, 1) / res_m
    return float(math.degrees(math.atan(math.hypot(dz_dx, dz_dy))))


# --- bounded LRU -----------------------------------------------------------


class _TileLRU:
    """LRU of decoded float32 tiles. Tracks peak residency for TLC-13 tests."""

    def __init__(self, cap: int) -> None:
        self._cap = max(1, cap)
        self._d: OrderedDict[tuple[int, int, int], np.ndarray] = OrderedDict()
        self.max_resident = 0

    def get(self, key: tuple[int, int, int]) -> np.ndarray | None:
        arr = self._d.get(key)
        if arr is not None:
            self._d.move_to_end(key)
        return arr

    def put(self, key: tuple[int, int, int], arr: np.ndarray) -> None:
        self._d[key] = arr
        self._d.move_to_end(key)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)  # evict least-recently-used
        self.max_resident = max(self.max_resident, len(self._d))

    def __len__(self) -> int:
        return len(self._d)


# --- sampler ---------------------------------------------------------------


class TerrainSampler:
    """Per-point slope sampler backed by the bounded LRU. No full-raster path."""

    def __init__(self, settings: Settings, zoom: int | None = None) -> None:
        self._s = settings
        self._zoom = zoom if zoom is not None else settings.terrarium_zoom
        assert settings.tile_cache_dir is not None
        self._cache = DiskCache(
            settings.tile_cache_dir, settings.tile_cache_ttl_s, suffix=".png"
        )
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=settings.http_concurrency,
            max_retries=settings.http_max_retries,
        )
        self._lru = _TileLRU(settings.tile_lru_cap)
        self.tiles_used: set[tuple[int, int, int]] = set()

    @property
    def zoom(self) -> int:
        return self._zoom

    @property
    def max_resident_tiles(self) -> int:
        return self._lru.max_resident

    async def slope_deg(self, lon: float, lat: float) -> float:
        """Slope (degrees) at a point. Raises TileUnreachable/TileDecodeError;
        the feature layer catches these and marks slope unavailable (TLC-6)."""
        xf, yf = lonlat_to_tile(lon, lat, self._zoom)
        tx, ty = int(math.floor(xf)), int(math.floor(yf))
        elev = await self._tile(tx, ty)
        px = (xf - tx) * TILE_PX
        py = (yf - ty) * TILE_PX
        return slope_at_pixel(elev, px, py, lat, self._zoom)

    async def _tile(self, tx: int, ty: int) -> np.ndarray:
        key = (self._zoom, tx, ty)
        cached = self._lru.get(key)
        if cached is not None:
            return cached
        disk_key = f"terrarium/{self._zoom}/{tx}/{ty}"
        raw = self._cache.get(disk_key)
        if raw is None:
            raw = await self._fetch(tx, ty)
            self._cache.put(disk_key, raw)
        arr = decode_terrarium(raw)
        self._lru.put(key, arr)
        self.tiles_used.add(key)
        return arr

    async def _fetch(self, tx: int, ty: int) -> bytes:
        url = f"{self._s.terrarium_url}/{self._zoom}/{tx}/{ty}.png"
        try:
            resp = await self._http.get(url, timeout=self._s.tile_timeout_s)
        except httpx.HTTPError as exc:
            raise TileUnreachable(f"tile {self._zoom}/{tx}/{ty}: {exc}") from exc
        if resp.status_code != 200:
            raise TileUnreachable(
                f"tile {self._zoom}/{tx}/{ty} -> {resp.status_code}"
            )
        return resp.content

    async def aclose(self) -> None:
        await self._http.aclose()
