"""SpatiaLite store (FC-6).

Single `Store` per process. Opens one aiosqlite connection with `mod_spatialite`
loaded, WAL + foreign keys on, and R*Tree spatial indexes on every geometry
column. All mutations go through repository methods here; no raw SQL elsewhere
(FC-4 / FC-6). Done runs are append-only (TLC-9); ingest upserts are
idempotent (TLC-11).

Schema lives in `schema.sql` (plain DDL) plus geometry-column / spatial-index
creation done here after `InitSpatialMetaData` (those calls need spatial
metadata to exist first). Versioned via `schema_version`; the store refuses to
start against a newer, unrecognized version.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import aiosqlite

from cairn.config import Settings
from cairn.engine.scoring_config import DEFAULT_SCORING_PARAMS, SCORING_CONFIG_VERSION
from cairn.engine.types import Candidate, ScoredCandidate, Way
from cairn.errors import (
    RunNotFound,
    SchemaVersionError,
    SpatialiteUnavailable,
    StoreError,
)
from cairn.store.types import Bbox, Run

_M_PER_DEG = 111320.0

SCHEMA_VERSION = 7
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Sentinel for partial updates: distinguishes "field omitted" from "set to NULL".
UNSET: object = object()

# Forward migrations keyed by from_version. Each entry is a list of statements
# ending with `INSERT INTO schema_version (version) VALUES (n+1)`.
_MIGRATIONS: dict[int, list[str]] = {
    1: [
        "ALTER TABLE candidate_scores ADD COLUMN is_distinct INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE candidate_scores ADD COLUMN distinct_rank INTEGER",
        "ALTER TABLE runs ADD COLUMN distinct_count INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (2)",
    ],
    2: [
        "ALTER TABLE candidate_features ADD COLUMN road_score REAL",
        "ALTER TABLE candidate_features ADD COLUMN road_unavailable INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (3)",
    ],
    3: [
        "ALTER TABLE candidate_features ADD COLUMN local_road_m REAL",
        "ALTER TABLE candidate_features ADD COLUMN remoteness_unavailable "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE candidate_features ADD COLUMN road_rough INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (4)",
    ],
    4: [
        "ALTER TABLE candidate_features ADD COLUMN dist_terminus_m REAL",
        "ALTER TABLE candidate_features ADD COLUMN road_end_unavailable "
        "INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO schema_version (version) VALUES (5)",
    ],
    5: [
        "CREATE TABLE IF NOT EXISTS saved_places ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT, "
        "  lat REAL NOT NULL, lon REAL NOT NULL, payload_json TEXT, note TEXT, "
        "  created_at TEXT NOT NULL)",
        "INSERT INTO schema_version (version) VALUES (6)",
    ],
    6: [
        "ALTER TABLE saved_places ADD COLUMN rating REAL",  # 0-5 stars, nullable
        "INSERT INTO schema_version (version) VALUES (7)",
    ],
}

# Geometry columns to attach after metadata init: table -> SpatiaLite type.
_GEOMETRY_COLUMNS: dict[str, str] = {
    "osm_features": "GEOMETRY",  # mixed point/line/polygon
    "candidates": "POINT",
}


class Store:
    """Async SpatiaLite repository."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._db: aiosqlite.Connection | None = None

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    async def open(cls, settings: Settings) -> "Store":
        store = cls(settings)
        await store.connect()
        await store.init()
        return store

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise StoreError("store is not connected")
        return self._db

    async def connect(self) -> None:
        s = self._s
        assert s.db_path is not None  # resolved by Settings validator
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(s.db_path)
        db.row_factory = aiosqlite.Row
        try:
            await self._load_spatialite(db)
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 5000")
        except SpatialiteUnavailable:
            await db.close()
            raise
        except aiosqlite.Error as exc:  # pragma: no cover - defensive
            await db.close()
            raise StoreError(f"failed to configure connection: {exc}") from exc
        self._db = db

    async def _load_spatialite(self, db: aiosqlite.Connection) -> None:
        lib = os.environ.get("SPATIALITE_LIBRARY_PATH", "mod_spatialite")
        try:
            await db.enable_load_extension(True)
            await db.load_extension(lib)
            await db.enable_load_extension(False)
        except (aiosqlite.Error, AttributeError) as exc:
            raise SpatialiteUnavailable(
                f"could not load SpatiaLite extension ({lib!r}): {exc}"
            ) from exc

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- schema init -------------------------------------------------------

    async def init(self) -> None:
        s = self._s
        for d in (s.tile_cache_dir, s.osm_cache_dir):
            assert d is not None
            d.mkdir(parents=True, exist_ok=True)
        db = self.db
        try:
            await self._ensure_spatial_metadata(db)
            await db.executescript(_SCHEMA_PATH.read_text())
            await self._ensure_geometry_columns(db)
            await self._seed_scoring_config(db)
            await self._apply_migrations(db)
            await db.commit()
        except aiosqlite.Error as exc:
            raise StoreError(f"schema init failed: {exc}") from exc

    async def _ensure_spatial_metadata(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spatial_ref_sys'"
        )
        fresh = await cur.fetchone() is None
        if fresh:
            # `1` = run inside a transaction; populates spatial_ref_sys once.
            await db.execute("SELECT InitSpatialMetaData(1)")

    async def _ensure_geometry_columns(self, db: aiosqlite.Connection) -> None:
        for table, gtype in _GEOMETRY_COLUMNS.items():
            cur = await db.execute(
                "SELECT 1 FROM geometry_columns "
                "WHERE f_table_name = ? AND f_geometry_column = 'geom'",
                (table,),
            )
            if await cur.fetchone() is None:
                await db.execute(
                    "SELECT AddGeometryColumn(?, 'geom', 4326, ?, 'XY')",
                    (table, gtype),
                )
                await db.execute("SELECT CreateSpatialIndex(?, 'geom')", (table,))

    async def _seed_scoring_config(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO scoring_config (version, params_json) VALUES (?, ?)",
            (SCORING_CONFIG_VERSION, json.dumps(DEFAULT_SCORING_PARAMS)),
        )

    async def _apply_migrations(self, db: aiosqlite.Connection) -> None:
        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row and row[0] is not None else None
        if current is None:
            await db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            return
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )
        while current < SCHEMA_VERSION:
            statements = _MIGRATIONS.get(current)
            if statements is None:
                raise SchemaVersionError(
                    f"no migration path from schema v{current}"
                )
            for stmt in statements:
                await db.execute(stmt)
            current += 1

    # --- health (FC-8) -----------------------------------------------------

    async def health(self) -> dict:
        """Confirm SpatiaLite loads, schema is present, caches are writable."""
        db = self.db
        cur = await db.execute("SELECT spatialite_version()")
        spatialite_version = (await cur.fetchone())[0]
        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        schema_version = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM scoring_config")
        scoring_configs = (await cur.fetchone())[0]
        s = self._s
        caches_writable = all(
            d is not None and os.access(d, os.W_OK)
            for d in (s.tile_cache_dir, s.osm_cache_dir)
        )
        return {
            "spatialite_version": spatialite_version,
            "schema_version": schema_version,
            "scoring_config_versions": scoring_configs,
            "caches_writable": caches_writable,
            "db_path": str(s.db_path),
        }

    # --- scoring config ----------------------------------------------------

    async def scoring_config_params(self, version: int) -> dict:
        cur = await self.db.execute(
            "SELECT params_json FROM scoring_config WHERE version = ?", (version,)
        )
        row = await cur.fetchone()
        if row is None:
            raise StoreError(f"no scoring_config version {version}")
        return json.loads(row[0])

    # --- runs (TLC-9 append-only once done) --------------------------------

    async def create_run(
        self, bbox: Bbox, weights: dict[str, float], scoring_config_version: int
    ) -> int:
        b = bbox.normalized()
        cur = await self.db.execute(
            "INSERT INTO runs (min_lon, min_lat, max_lon, max_lat, weights_json, "
            "scoring_config_version, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (
                b.min_lon,
                b.min_lat,
                b.max_lon,
                b.max_lat,
                json.dumps(weights),
                scoring_config_version,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def set_run_status(self, run_id: int, status: str) -> None:
        await self.db.execute(
            "UPDATE runs SET status = ? WHERE id = ?", (status, run_id)
        )
        await self.db.commit()

    async def fail_run(self, run_id: int, reason: str) -> None:
        await self.db.execute(
            "UPDATE runs SET status = 'failed', failure_reason = ? WHERE id = ?",
            (reason, run_id),
        )
        await self.db.commit()

    async def finish_run(
        self,
        run_id: int,
        *,
        provenance: dict,
        candidate_count: int,
        distinct_count: int,
        capped: bool,
        degraded_features: list[str],
    ) -> None:
        await self.db.execute(
            "UPDATE runs SET status = 'done', provenance_json = ?, candidate_count = ?, "
            "distinct_count = ?, capped = ?, degraded_features_json = ?, "
            "done_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (
                json.dumps(provenance),
                candidate_count,
                distinct_count,
                1 if capped else 0,
                json.dumps(degraded_features),
                run_id,
            ),
        )
        await self.db.commit()

    async def get_run(self, run_id: int) -> Run:
        cur = await self.db.execute(
            "SELECT id, min_lon, min_lat, max_lon, max_lat, weights_json, "
            "scoring_config_version, provenance_json, status, failure_reason, "
            "candidate_count, capped, degraded_features_json, created_at, done_at, "
            "distinct_count "
            "FROM runs WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        return Run(
            id=row[0],
            bbox=Bbox(row[1], row[2], row[3], row[4]),
            weights=json.loads(row[5]),
            scoring_config_version=row[6],
            provenance=json.loads(row[7]) if row[7] else None,
            status=row[8],
            failure_reason=row[9],
            candidate_count=row[10],
            capped=bool(row[11]),
            degraded_features=json.loads(row[12]) if row[12] else [],
            created_at=row[13],
            done_at=row[14],
            distinct_count=row[15],
        )

    async def list_runs(self, limit: int = 50) -> list[Run]:
        cur = await self.db.execute(
            "SELECT id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        ids = [r[0] for r in await cur.fetchall()]
        return [await self.get_run(i) for i in ids]

    # --- Saved places (favorites — user data, never scoring) ----------------

    @staticmethod
    def _saved_row(row) -> dict:
        return {
            "id": row[0], "kind": row[1], "name": row[2], "lat": row[3], "lon": row[4],
            "payload": json.loads(row[5]) if row[5] else None,
            "note": row[6], "created_at": row[7], "rating": row[8],
        }

    _SAVED_COLS = "id, kind, name, lat, lon, payload_json, note, created_at, rating"

    async def add_saved(
        self, kind: str, name: str | None, lat: float, lon: float,
        payload: dict | None = None, note: str | None = None, rating: float | None = None,
    ) -> dict:
        import datetime as _dt

        created = _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()
        cur = await self.db.execute(
            "INSERT INTO saved_places "
            "(kind, name, lat, lon, payload_json, note, created_at, rating) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, name, lat, lon,
             json.dumps(payload) if payload is not None else None, note, created, rating),
        )
        await self.db.commit()
        return self._saved_row(
            await (await self.db.execute(
                f"SELECT {self._SAVED_COLS} FROM saved_places WHERE id=?", (cur.lastrowid,)
            )).fetchone()
        )

    async def list_saved(self) -> list[dict]:
        cur = await self.db.execute(
            f"SELECT {self._SAVED_COLS} FROM saved_places ORDER BY created_at DESC, id DESC"
        )
        return [self._saved_row(r) for r in await cur.fetchall()]

    async def delete_saved(self, saved_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM saved_places WHERE id=?", (saved_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def update_saved(
        self, saved_id: int, name: object = UNSET, rating: object = UNSET,
        note: object = UNSET,
    ) -> dict | None:
        """Partial update of a saved place. Only fields not left UNSET are
        written (so editing one of name/rating/note doesn't clobber the rest)."""
        sets, params = [], []
        if name is not UNSET:
            sets.append("name=?")
            params.append(name)
        if rating is not UNSET:
            sets.append("rating=?")
            params.append(rating)
        if note is not UNSET:
            sets.append("note=?")
            params.append(note)
        if sets:
            params.append(saved_id)
            await self.db.execute(
                f"UPDATE saved_places SET {', '.join(sets)} WHERE id=?", params
            )
            await self.db.commit()
        row = await (await self.db.execute(
            f"SELECT {self._SAVED_COLS} FROM saved_places WHERE id=?", (saved_id,)
        )).fetchone()
        return self._saved_row(row) if row else None

    # --- Site name suggestion (from our own OSM extract — TLC-3, display) ----

    async def _nearest_named(
        self, lat: float, lon: float, feature_class: str, max_m: float
    ) -> str | None:
        """Nearest osm_feature of `feature_class` with a name/ref within
        `max_m`, by geodesic distance — used to auto-name a scored site."""
        import math as _math

        rdeg = max_m / (111320.0 * max(_math.cos(_math.radians(lat)), 0.1))
        cur = await self.db.execute(
            "SELECT tags_json, ST_Distance(MakePoint(?, ?, 4326), geom, 1) AS d "
            "FROM osm_features WHERE feature_class=? AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='osm_features' "
            "  AND f_geometry_column='geom' AND search_frame=BuildCircleMbr(?, ?, ?, 4326)) "
            "ORDER BY d LIMIT 20",
            (lon, lat, feature_class, lon, lat, rdeg),
        )
        for tags_json, d in await cur.fetchall():
            if d is None or d > max_m:
                continue
            try:
                tags = json.loads(tags_json) if tags_json else {}
            except (ValueError, TypeError):
                tags = {}
            nm = tags.get("name") or tags.get("ref")
            if nm:
                return str(nm)
        return None

    async def suggest_site_name(self, lat: float, lon: float) -> str | None:
        """A friendly default name for a scored site from Cairn's own data:
        the nearest named lake (the lakeside-spur niche) else the source road.
        None if neither — the caller may fall back to reverse geocoding."""
        water = await self._nearest_named(lat, lon, "water", 1500)
        if water:
            return f"Near {water}"
        return await self._nearest_named(lat, lon, "drivable", 250)

    # --- OSM ingest (chunked, idempotent — TLC-11, TLC-14) -----------------

    async def upsert_osm_chunk(self, rows: list[tuple]) -> None:
        """Upsert a chunk of (osm_type, osm_id, feature_class, tags_json, wkt).

        Keyed on (osm_type, osm_id, feature_class) so re-ingest is a no-op on
        content match (TLC-11). One commit per chunk (TLC-14).
        """
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO osm_features (osm_type, osm_id, feature_class, tags_json, geom) "
            "VALUES (?, ?, ?, ?, GeomFromText(?, 4326)) "
            "ON CONFLICT(osm_type, osm_id, feature_class) DO UPDATE SET "
            "tags_json = excluded.tags_json, geom = excluded.geom",
            rows,
        )
        await self.db.commit()

    async def classes_present(self, bbox: Bbox) -> set[str]:
        """Which scored feature classes exist within the bbox (TLC-6 layer
        availability). Uses the R*Tree MBR filter."""
        b = bbox.normalized()
        cur = await self.db.execute(
            "SELECT DISTINCT feature_class FROM osm_features "
            "WHERE ROWID IN (SELECT ROWID FROM SpatialIndex "
            "  WHERE f_table_name = 'osm_features' AND f_geometry_column = 'geom' "
            "  AND search_frame = BuildMbr(?, ?, ?, ?, 4326))",
            (b.min_lon, b.min_lat, b.max_lon, b.max_lat),
        )
        return {r[0] for r in await cur.fetchall()}

    async def load_drivable_ways(self, bbox: Bbox, clip: bool = True) -> list[Way]:
        """Drivable ways intersecting the bbox, as ordered polylines.

        `clip=True` (default): clip each way to the bbox (ST_Intersection) so
        candidates never land outside the requested area; a clipped way that
        splits into disjoint parts becomes several Ways (same osm_id).
        `clip=False`: return whole (unclipped) geometry — used for terminus
        detection, where bbox-edge cuts must not be mistaken for dead-ends."""
        b = bbox.normalized()
        geom_expr = (
            "AsGeoJSON(ST_Intersection(geom, BuildMbr(?, ?, ?, ?, 4326)))"
            if clip
            else "AsGeoJSON(geom)"
        )
        clip_params = (
            (b.min_lon, b.min_lat, b.max_lon, b.max_lat) if clip else ()
        )
        cur = await self.db.execute(
            f"SELECT osm_id, {geom_expr} FROM osm_features "
            "WHERE feature_class = 'drivable' AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name = 'osm_features' "
            "  AND f_geometry_column = 'geom' "
            "  AND search_frame = BuildMbr(?, ?, ?, ?, 4326))",
            (*clip_params, b.min_lon, b.min_lat, b.max_lon, b.max_lat),
        )
        ways: list[Way] = []
        for osm_id, gj in await cur.fetchall():
            if not gj:
                continue
            for part in _line_parts(json.loads(gj)):
                if len(part) >= 2:
                    ways.append(Way(osm_id=int(osm_id), coords=tuple(part)))
        return ways

    async def drivable_way_tags(self, bbox: Bbox) -> dict[int, dict]:
        """`{osm_id: tags}` for drivable ways in the bbox — the source-road tags
        the road_character feature reads (FC-4). Fetched once per run."""
        b = bbox.normalized()
        cur = await self.db.execute(
            "SELECT osm_id, tags_json FROM osm_features "
            "WHERE feature_class = 'drivable' AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name = 'osm_features' "
            "  AND f_geometry_column = 'geom' "
            "  AND search_frame = BuildMbr(?, ?, ?, ?, 4326))",
            (b.min_lon, b.min_lat, b.max_lon, b.max_lat),
        )
        out: dict[int, dict] = {}
        for osm_id, tags_json in await cur.fetchall():
            try:
                out[int(osm_id)] = json.loads(tags_json) if tags_json else {}
            except (ValueError, TypeError):
                out[int(osm_id)] = {}
        return out

    async def local_road_length_m(
        self, lon: float, lat: float, radius_m: float
    ) -> float:
        """Total drivable-road length (m) within a ~radius_m box of the point —
        the local road-density proxy for the remoteness feature (FC-4). Lower =
        more isolated. Roads are clipped to the box (ST_Intersection) so only
        the nearby length counts; the R*Tree bounds the work."""
        import math as _math

        cos_lat = _math.cos(_math.radians(lat))
        rd_lon = radius_m / (_M_PER_DEG * max(cos_lat, 1e-6))
        rd_lat = radius_m / _M_PER_DEG
        box = (lon - rd_lon, lat - rd_lat, lon + rd_lon, lat + rd_lat)
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(GeodesicLength(ST_Intersection(geom, "
            "  BuildMbr(?, ?, ?, ?, 4326)))), 0) FROM osm_features "
            "WHERE feature_class = 'drivable' AND ROWID IN ("
            "  SELECT ROWID FROM SpatialIndex WHERE f_table_name = 'osm_features' "
            "  AND f_geometry_column = 'geom' AND search_frame = BuildMbr(?, ?, ?, ?, 4326))",
            (*box, *box),
        )
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    # --- candidates / features / scores ------------------------------------

    async def insert_candidates(
        self, run_id: int, candidates: list[Candidate], capped: bool
    ) -> list[tuple[int, Candidate]]:
        """Insert candidates; return (db_id, Candidate) pairs in insert order."""
        if not candidates:
            return []
        await self.db.executemany(
            "INSERT INTO candidates (run_id, source_way_id, capped, lon, lat, geom) "
            "VALUES (?, ?, ?, ?, ?, MakePoint(?, ?, 4326))",
            [
                (run_id, c.source_way_id, 1 if capped else 0, c.lon, c.lat, c.lon, c.lat)
                for c in candidates
            ],
        )
        await self.db.commit()
        cur = await self.db.execute(
            "SELECT id, lon, lat, source_way_id FROM candidates "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [
            (row[0], Candidate(lon=row[1], lat=row[2], source_way_id=row[3]))
            for row in await cur.fetchall()
        ]

    async def nearest_distance_m(
        self, lon: float, lat: float, classes: tuple[str, ...], max_radius_m: float
    ) -> float | None:
        """Min geodesic distance (m) from a point to any feature in `classes`
        within `max_radius_m`. None if none found in the search frame.

        Inside a polygon (e.g. a forest) ST_Distance is 0. The R*Tree MBR
        bounds the work to O(local features), never the whole layer."""
        import math as _math

        cos_lat = _math.cos(_math.radians(lat))
        radius_deg = max_radius_m / (_M_PER_DEG * max(cos_lat, 1e-6))
        placeholders = ",".join("?" for _ in classes)
        cur = await self.db.execute(
            f"SELECT MIN(ST_Distance(MakePoint(?, ?, 4326), geom, 1)) "
            f"FROM osm_features WHERE feature_class IN ({placeholders}) AND ROWID IN ("
            f"  SELECT ROWID FROM SpatialIndex WHERE f_table_name = 'osm_features' "
            f"  AND f_geometry_column = 'geom' "
            f"  AND search_frame = BuildCircleMbr(?, ?, ?, 4326))",
            (lon, lat, *classes, lon, lat, radius_deg),
        )
        row = await cur.fetchone()
        return None if row is None or row[0] is None else float(row[0])

    async def insert_candidate_features(self, rows: list[tuple]) -> None:
        """rows: (candidate_id, slope_deg, slope_unavail, dist_water_m,
        water_unavail, dist_forest_m, forest_unavail, dist_solitude_m,
        solitude_unavail, near_campsite, road_score, road_unavail,
        local_road_m, road_rough, dist_terminus_m, road_end_unavail)."""
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO candidate_features (candidate_id, slope_deg, slope_unavailable, "
            "dist_water_m, water_unavailable, dist_forest_m, forest_unavailable, "
            "dist_solitude_m, solitude_unavailable, near_campsite, road_score, "
            "road_unavailable, local_road_m, road_rough, dist_terminus_m, "
            "road_end_unavailable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def insert_candidate_scores(
        self, scored: list[ScoredCandidate], id_map: dict[tuple, int]
    ) -> None:
        rows = []
        for sc in scored:
            key = (sc.candidate.lon, sc.candidate.lat, sc.candidate.source_way_id)
            cid = id_map.get(key)
            if cid is None:
                continue
            contributions = [
                {
                    "feature": c.feature,
                    "normalized": c.normalized,
                    "weight": c.weight,
                    "contribution": c.contribution,
                }
                for c in sc.contributions
            ]
            rows.append(
                (
                    cid,
                    sc.total,
                    json.dumps(contributions),
                    json.dumps(sc.degraded_features),
                    1 if sc.distinct else 0,
                    sc.distinct_rank,
                )
            )
        if not rows:
            return
        await self.db.executemany(
            "INSERT INTO candidate_scores (candidate_id, total, contributions_json, "
            "degraded_features_json, is_distinct, distinct_rank) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def get_run_geojson(self, run_id: int, top_n: int | None = None) -> dict:
        """A GeoJSON FeatureCollection of scored candidates, ranked, carrying
        the TLC-3 disclaimer, TLC-8 contributions, TLC-6 degraded flags, and
        TLC-2 provenance. Reads committed results from the DB (TLC-7)."""
        from cairn import DISCLAIMER

        run = await self.get_run(run_id)
        limit_clause = "LIMIT ?" if top_n else ""
        params: tuple = (run_id, top_n) if top_n else (run_id,)
        cur = await self.db.execute(
            "SELECT c.id, c.lon, c.lat, c.source_way_id, s.total, s.contributions_json, "
            "s.degraded_features_json, f.slope_deg, f.dist_water_m, f.dist_forest_m, "
            "f.dist_solitude_m, f.near_campsite, s.is_distinct, s.distinct_rank, "
            "f.road_score, f.local_road_m, f.road_rough, f.dist_terminus_m "
            "FROM candidates c JOIN candidate_scores s ON s.candidate_id = c.id "
            "LEFT JOIN candidate_features f ON f.candidate_id = c.id "
            f"WHERE c.run_id = ? ORDER BY s.total DESC, c.id {limit_clause}",
            params,
        )
        features = []
        for rank, row in enumerate(await cur.fetchall(), start=1):
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [row[1], row[2]]},
                    "properties": {
                        "rank": rank,
                        "candidate_id": row[0],
                        "source_way_id": row[3],
                        "total": row[4],
                        "contributions": json.loads(row[5]),
                        "degraded_features": json.loads(row[6]),
                        "slope_deg": row[7],
                        "dist_water_m": row[8],
                        "dist_forest_m": row[9],
                        "dist_solitude_m": row[10],
                        "near_campsite": bool(row[11]),
                        "distinct": bool(row[12]),
                        "distinct_rank": row[13],
                        "road_score": row[14],
                        "local_road_m": row[15],
                        "rough": bool(row[16]),
                        "dist_terminus_m": row[17],
                        "disclaimer": DISCLAIMER,
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "disclaimer": DISCLAIMER,
            "run": {
                "id": run.id,
                "status": run.status,
                "bbox": [
                    run.bbox.min_lon,
                    run.bbox.min_lat,
                    run.bbox.max_lon,
                    run.bbox.max_lat,
                ],
                "weights": run.weights,
                "scoring_config_version": run.scoring_config_version,
                "provenance": run.provenance,
                "candidate_count": run.candidate_count,
                "distinct_count": run.distinct_count,
                "capped": run.capped,
                "degraded_features": run.degraded_features,
                "failure_reason": run.failure_reason,
            },
            "features": features,
        }

    async def get_run_roads_geojson(self, run_id: int) -> dict:
        """The run's drivable ways (OSM ∪ MVUM) as a GeoJSON LineString
        FeatureCollection, clipped to the run bbox — the road data Cairn
        actually scored. Rendered as a vector overlay so forest spurs show
        crisply at every zoom and on the satellite basemap, where Esri's
        transportation reference layer carries no forest roads. Carries the
        TLC-3 disclaimer; suitability not legality."""
        from cairn import DISCLAIMER
        from cairn.engine.features import road_is_rough

        run = await self.get_run(run_id)
        ways = await self.load_drivable_ways(run.bbox, clip=True)
        tags_by_id = await self.drivable_way_tags(run.bbox)

        features = []
        for way in ways:
            tags = tags_by_id.get(way.osm_id, {})
            operator = (tags.get("operator") or "").lower()
            is_forest = "mvum_access" in tags or "forest service" in operator
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(c) for c in way.coords],
                    },
                    "properties": {
                        "osm_id": way.osm_id,
                        "highway": tags.get("highway"),
                        "name": tags.get("name") or tags.get("ref"),
                        "forest_road": is_forest,
                        "rough": road_is_rough(tags),
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "disclaimer": DISCLAIMER,
            "run": {"id": run.id, "status": run.status},
            "features": features,
        }


def _line_parts(geom: dict) -> list[list[tuple[float, float]]]:
    """Ordered (lon, lat) parts from a GeoJSON LineString/MultiLineString/
    GeometryCollection — one list per continuous linestring."""
    gtype = geom.get("type")
    if gtype == "LineString":
        return [[(float(x), float(y)) for x, y in geom.get("coordinates", [])]]
    if gtype == "MultiLineString":
        return [
            [(float(x), float(y)) for x, y in part]
            for part in geom.get("coordinates", [])
        ]
    if gtype == "GeometryCollection":
        out: list[list[tuple[float, float]]] = []
        for g in geom.get("geometries", []):
            out.extend(_line_parts(g))
        return out
    return []
