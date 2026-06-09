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
from cairn.errors import SchemaVersionError, SpatialiteUnavailable, StoreError

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Forward migrations keyed by from_version. Empty at v=1; each entry is a list
# of statements ending with `INSERT INTO schema_version (version) VALUES (n+1)`.
_MIGRATIONS: dict[int, list[str]] = {}

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
