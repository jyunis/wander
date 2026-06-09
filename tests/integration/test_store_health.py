"""Store init + health (FC-6, FC-8).

Confirms the SpatiaLite extension loads, the schema initializes to the current
version, geometry columns + spatial indexes exist, and the scoring_config seed
landed. This is the milestone-1 acceptance gate for the store.
"""

from __future__ import annotations

from cairn.store import SCHEMA_VERSION, Store


async def test_health_reports_spatialite_and_schema(store: Store) -> None:
    info = await store.health()
    assert info["spatialite_version"]  # non-empty version string
    assert info["schema_version"] == SCHEMA_VERSION
    assert info["scoring_config_versions"] >= 1
    assert info["caches_writable"] is True


async def test_geometry_columns_and_spatial_index_created(store: Store) -> None:
    db = store.db
    cur = await db.execute(
        "SELECT f_table_name FROM geometry_columns ORDER BY f_table_name"
    )
    tables = {row[0] for row in await cur.fetchall()}
    assert {"osm_features", "candidates"} <= tables

    # CreateSpatialIndex makes an idx_<table>_geom R*Tree virtual table.
    for table in ("osm_features", "candidates"):
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (f"idx_{table}_geom",),
        )
        assert await cur.fetchone() is not None, f"missing spatial index for {table}"


async def test_init_is_idempotent(store: Store) -> None:
    # Re-running init against an already-initialized DB must not error or
    # duplicate the scoring_config seed.
    await store.init()
    info = await store.health()
    assert info["scoring_config_versions"] == 1


async def test_spatialite_distance_callable(store: Store) -> None:
    # A geodesic distance in meters between two points ~ smoke test that the
    # spatial functions Cairn relies on (FC-4) are actually wired up.
    db = store.db
    cur = await db.execute(
        "SELECT ST_Distance("
        "  MakePoint(-91.86, 47.90, 4326),"
        "  MakePoint(-91.85, 47.90, 4326), 1)"
    )
    meters = (await cur.fetchone())[0]
    assert 600 < meters < 900  # ~745 m at this latitude
