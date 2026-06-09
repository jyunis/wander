"""Synthetic Terrarium tile encoder — the inverse of clients.terrain.decode.

Lets tile/slope tests run fully offline against known elevation fields.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from cairn.clients.terrain import TILE_PX


def encode_terrarium(elev: np.ndarray) -> bytes:
    """Encode a (TILE_PX, TILE_PX) elevation array to a Terrarium PNG."""
    assert elev.shape == (TILE_PX, TILE_PX)
    total = np.rint((elev + 32768.0) * 256.0).astype(np.int64)
    total = np.clip(total, 0, 256**3 - 1)
    r = (total // 65536) % 256
    g = (total // 256) % 256
    b = total % 256
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG")
    return buf.getvalue()


def flat_tile(elevation: float = 100.0) -> np.ndarray:
    return np.full((TILE_PX, TILE_PX), elevation, dtype=np.float32)


def ramp_tile_x(meters_per_col: float) -> np.ndarray:
    """Elevation rising linearly with column index (a constant E–W slope)."""
    cols = np.arange(TILE_PX, dtype=np.float32) * meters_per_col
    return np.broadcast_to(cols, (TILE_PX, TILE_PX)).astype(np.float32)
