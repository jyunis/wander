"""Schema migration checks (trip planner v8, collections v9).

The fresh-DB path is covered by schema.sql (exercised by every store fixture);
here we verify the forward migration SQL in isolation, with plain sqlite3
(no SpatiaLite needed for these CREATE TABLEs).
"""

from __future__ import annotations

import sqlite3

from cairn.store.repo import _MIGRATIONS, SCHEMA_VERSION


def test_schema_version_is_11():
    assert SCHEMA_VERSION == 11


def test_v7_to_v8_migration_creates_trip_tables():
    stmts = _MIGRATIONS[7]
    assert stmts[-1] == "INSERT INTO schema_version (version) VALUES (8)"

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (7)")
    db.execute("CREATE TABLE saved_places (id INTEGER PRIMARY KEY)")  # FK target
    for stmt in stmts:
        db.execute(stmt)

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trips", "trip_members"} <= tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(trip_members)")}
    assert {"trip_id", "saved_id", "role", "position"} <= cols
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
    db.close()


def test_v8_to_v9_migration_creates_collection_tables():
    stmts = _MIGRATIONS[8]
    assert stmts[-1] == "INSERT INTO schema_version (version) VALUES (9)"

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (8)")
    db.execute("CREATE TABLE saved_places (id INTEGER PRIMARY KEY)")  # FK target
    for stmt in stmts:
        db.execute(stmt)

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"collections", "place_collections"} <= tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(place_collections)")}
    assert {"collection_id", "saved_id"} <= cols
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    db.close()


def test_v9_to_v10_migration_adds_runs_place():
    stmts = _MIGRATIONS[9]
    assert stmts[-1] == "INSERT INTO schema_version (version) VALUES (10)"

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (9)")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    for stmt in stmts:
        db.execute(stmt)

    cols = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    assert "place" in cols
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
    db.close()


def test_v10_to_v11_migration_creates_forest_marks():
    stmts = _MIGRATIONS[10]
    assert stmts[-1] == "INSERT INTO schema_version (version) VALUES (11)"

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (10)")
    for stmt in stmts:
        db.execute(stmt)

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "forest_marks" in tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(forest_marks)")}
    assert {"kind", "forest_key", "position"} <= cols
    assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
    db.close()
