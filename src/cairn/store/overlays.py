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
    "campground": "Campgrounds",
    "ownership": "Public land ownership",
    "national_forest": "National Forest",
    "state_forest": "State Forest",
    "state_park": "State Parks",
}
LAYER_ORDER = ["campground", "national_forest", "state_forest", "state_park", "ownership"]

# Point layers live in `overlay_points` (markers); everything else is a polygon
# layer in `overlay_features`.
POINT_LAYERS = {"campground"}

DISCLAIMER = (
    "Land reference layers (ownership, boundaries, campgrounds) are public data "
    "shown for context only. They do NOT establish that camping is legal "
    "anywhere — always verify access and regulations with the managing agency."
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

    async def _table_layer_counts(self, table: str) -> dict[str, int]:
        """`{layer: count}` for a table, or {} if the table doesn't exist."""
        if self._db is None:
            return {}
        exists = await (
            await self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
        ).fetchone()
        if not exists:
            return {}
        cur = await self._db.execute(f"SELECT layer, COUNT(*) FROM {table} GROUP BY layer")
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def layers(self) -> list[dict]:
        """Available layers with geometry type, feature count + distinct
        categories (legend) — unioned across the polygon and point tables."""
        if self._db is None:
            return []
        poly = await self._table_layer_counts("overlay_features")
        pts = await self._table_layer_counts("overlay_points")
        counts = {**poly, **pts}
        out = []
        for lid in [*LAYER_ORDER, *(c for c in counts if c not in LAYER_ORDER)]:
            if lid not in counts:
                continue
            table = "overlay_points" if lid in pts else "overlay_features"
            cur = await self._db.execute(
                f"SELECT DISTINCT category FROM {table} "
                "WHERE layer=? AND category IS NOT NULL ORDER BY category",
                (lid,),
            )
            out.append(
                {
                    "id": lid,
                    "title": LAYER_TITLES.get(lid, lid),
                    "geom_type": "point" if lid in pts else "polygon",
                    "count": counts[lid],
                    "categories": [r[0] for r in await cur.fetchall()],
                }
            )
        return out

    async def query(self, layer: str, bbox: Bbox) -> dict:
        """GeoJSON FeatureCollection of `layer` within `bbox`, capped at
        `overlay_max_features` (with a `truncated` flag), carrying the
        display-only disclaimer (TLC-3). Polygon layers are clipped to the bbox
        so payloads stay bounded; point layers (campgrounds) return whole points
        with a reservation `url` where known."""
        empty = {"type": "FeatureCollection", "features": [], "disclaimer": DISCLAIMER}
        if self._db is None:
            return empty
        b = bbox.normalized()
        frame = (b.min_lon, b.min_lat, b.max_lon, b.max_lat)
        is_point = layer in POINT_LAYERS
        if is_point:
            sql = (
                "SELECT category, name, url, meta, AsGeoJSON(geom) FROM overlay_points "
                "WHERE layer=? AND ROWID IN ("
                "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_points' "
                "  AND f_geometry_column='geom' AND search_frame=BuildMbr(?, ?, ?, ?, 4326)) "
                "LIMIT ?"
            )
            params = (layer, *frame, self._max + 1)
        else:
            sql = (
                "SELECT category, name, NULL, NULL, "
                "  AsGeoJSON(ST_Intersection(geom, BuildMbr(?, ?, ?, ?, 4326))) "
                "FROM overlay_features WHERE layer=? AND ROWID IN ("
                "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_features' "
                "  AND f_geometry_column='geom' AND search_frame=BuildMbr(?, ?, ?, ?, 4326)) "
                "LIMIT ?"
            )
            params = (*frame, layer, *frame, self._max + 1)
        rows = await (await self._db.execute(sql, params)).fetchall()
        truncated = len(rows) > self._max
        features = []
        for category, name, url, meta, gj in rows[: self._max]:
            if not gj:
                continue
            props = {"layer": layer, "category": category, "name": name}
            if url:
                props["url"] = url
            if meta:
                props["meta"] = json.loads(meta)
            features.append({"type": "Feature", "geometry": json.loads(gj), "properties": props})
        return {
            "type": "FeatureCollection",
            "layer": layer,
            "geom_type": "point" if is_point else "polygon",
            "truncated": truncated,
            "features": features,
            "disclaimer": DISCLAIMER,
        }

    async def nearest_campgrounds(
        self, lat: float, lon: float, limit: int = 5, max_km: float = 80.0
    ) -> list[dict]:
        """The `limit` nearest campground points to (lat, lon) within `max_km`,
        nearest first, each `{name, category, url, meta, lon, lat, distance_m}`.
        Display-only (TLC-3): annotates a scored dispersed site with 'book a
        spot' fallbacks — NEVER read by scoring."""
        if self._db is None:
            return []
        rdeg = max_km / 111.0  # ~deg of latitude; generous R*Tree search frame
        cur = await self._db.execute(
            "SELECT name, category, url, meta, X(geom), Y(geom), "
            "  ST_Distance(MakePoint(?, ?, 4326), geom, 1) AS d "
            "FROM overlay_points WHERE layer='campground' AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_points' "
            "  AND f_geometry_column='geom' AND search_frame=BuildCircleMbr(?, ?, ?, 4326)) "
            "ORDER BY d LIMIT ?",
            (lon, lat, lon, lat, rdeg, limit),
        )
        out = []
        for name, category, url, meta, lon_, lat_, d in await cur.fetchall():
            if d is None:
                continue
            out.append(
                {
                    "name": name,
                    "category": category,
                    "url": url,
                    "meta": json.loads(meta) if meta else None,
                    "lon": lon_,
                    "lat": lat_,
                    "distance_m": float(d),
                }
            )
        return out
