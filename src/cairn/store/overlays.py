"""Read-only access to the static land-overlay DB (display-only — TLC-3).

`overlays.db` is a separate SpatiaLite file from `cairn.db`, produced by
`scripts/load_overlays.py` from public MnGeo / USFS data: public land
**ownership** (Federal / State / County / **Tax Forfeit** / Tribal) and
forest/park **boundaries**. These layers are NEVER scored or used to assert a
site is legal — Cairn renders them as visual context only (TLC-3). If the file
is absent the store reports no layers and serves empty results, so Cairn runs
fine without it.
"""

from __future__ import annotations

import json
import os

import aiosqlite

from cairn.config import Settings
from cairn.store.types import Bbox

# Display titles + a stable render order for the UI Land panel.
LAYER_TITLES: dict[str, str] = {
    "ownership": "Public land ownership",
    "national_forest": "National Forest",
    "state_forest": "State Forest",
    "state_park": "State Parks",
}
LAYER_ORDER = ["national_forest", "state_forest", "state_park", "ownership"]

DISCLAIMER = (
    "Land-ownership and boundary layers are public reference data shown for "
    "context only. They do NOT establish that camping is legal anywhere — "
    "always verify access and regulations with the managing agency."
)


class OverlayStore:
    """Opens `overlays.db` read-only if present; otherwise inert."""

    def __init__(self, settings: Settings) -> None:
        self._path = settings.overlays_db_path
        self._max = settings.overlay_max_features
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, settings: Settings) -> "OverlayStore":
        store = cls(settings)
        if store._path is not None and os.path.exists(store._path):
            await store._connect()
        return store

    async def _connect(self) -> None:
        db = await aiosqlite.connect(f"file:{self._path}?mode=ro", uri=True)
        lib = os.environ.get("SPATIALITE_LIBRARY_PATH", "mod_spatialite")
        await db.enable_load_extension(True)
        await db.load_extension(lib)
        await db.enable_load_extension(False)
        self._db = db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def available(self) -> bool:
        return self._db is not None

    async def layers(self) -> list[dict]:
        """Available layers with feature count + distinct categories (legend)."""
        if self._db is None:
            return []
        cur = await self._db.execute(
            "SELECT layer, COUNT(*) FROM overlay_features GROUP BY layer"
        )
        counts = {row[0]: row[1] for row in await cur.fetchall()}
        out = []
        for lid in [*LAYER_ORDER, *(c for c in counts if c not in LAYER_ORDER)]:
            if lid not in counts:
                continue
            cur = await self._db.execute(
                "SELECT DISTINCT category FROM overlay_features "
                "WHERE layer=? AND category IS NOT NULL ORDER BY category",
                (lid,),
            )
            cats = [r[0] for r in await cur.fetchall()]
            out.append(
                {
                    "id": lid,
                    "title": LAYER_TITLES.get(lid, lid),
                    "count": counts[lid],
                    "categories": cats,
                }
            )
        return out

    async def query(self, layer: str, bbox: Bbox) -> dict:
        """GeoJSON FeatureCollection of `layer` within `bbox`, each feature's
        geometry clipped to the bbox (so payloads stay bounded even for dense
        ownership parcels). Capped at `overlay_max_features` with a `truncated`
        flag. Carries the display-only disclaimer (TLC-3)."""
        if self._db is None:
            return {"type": "FeatureCollection", "features": [], "disclaimer": DISCLAIMER}
        b = bbox.normalized()
        frame = (b.min_lon, b.min_lat, b.max_lon, b.max_lat)
        cur = await self._db.execute(
            "SELECT category, name, "
            "  AsGeoJSON(ST_Intersection(geom, BuildMbr(?, ?, ?, ?, 4326))) "
            "FROM overlay_features "
            "WHERE layer = ? AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_features' "
            "  AND f_geometry_column='geom' AND search_frame=BuildMbr(?, ?, ?, ?, 4326)) "
            "LIMIT ?",
            (*frame, layer, *frame, self._max + 1),
        )
        rows = await cur.fetchall()
        truncated = len(rows) > self._max
        features = []
        for category, name, gj in rows[: self._max]:
            if not gj:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": json.loads(gj),
                    "properties": {"layer": layer, "category": category, "name": name},
                }
            )
        return {
            "type": "FeatureCollection",
            "layer": layer,
            "truncated": truncated,
            "features": features,
            "disclaimer": DISCLAIMER,
        }
