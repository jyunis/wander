"""Schema migration checks for the trip planner (v8).

The fresh-DB path is covered by schema.sql (exercised by every store fixture);
here we verify the v7→v8 forward migration SQL in isolation, with plain sqlite3
(no SpatiaLite needed for these CREATE TABLEs).
"""

from __future__ import annotations

import sqlite3

from cairn.store.repo import _MIGRATIONS, SCHEMA_VERSION


def test_schema_version_is_8():
    assert SCHEMA_VERSION == 8


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
