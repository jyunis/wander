"""One-time dedupe + re-key of MVUM drivable rows in cairn.db.

Background: MVUM ingest used to key rows on the ArcGIS `objectid`, which the
FeatureServer regenerates for the same physical road on every query. So each
re-score of an area piled another identical copy of its forest roads into the
`drivable` table. Those duplicates masked the roads' own dead-ends in
find_termini (a coincident copy reads as "another road connects here") and
inflated remoteness. ingest/mvum.py now keys by a stable geometry hash.

This script (a) collapses each set of identical-geometry rows to one, and (b)
re-keys the survivors to the SAME geometry-derived id the new ingest produces —
so a future re-score of an area upserts onto the existing row instead of adding a
parallel copy under a new key. Idempotent: a second run is a no-op. mvum only.

Run in-image with /data mounted (no network needed; writes the live cairn.db):

    docker run --rm -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data --entrypoint uv cairn:dev \
      run python scripts/dedupe_mvum_roads.py
"""

from __future__ import annotations

import json
import os
import sqlite3

from cairn.config import Settings
from cairn.ingest.mvum import stable_mvum_id

_WHERE = "osm_type = 'mvum' AND feature_class = 'drivable'"


def _geojson_to_paths(gj: str) -> list:
    """Stored geometry GeoJSON -> ArcGIS-style paths (list of coord lists), the
    shape stable_mvum_id() keys on."""
    g = json.loads(gj)
    if g["type"] == "LineString":
        return [g["coordinates"]]
    if g["type"] == "MultiLineString":
        return g["coordinates"]
    return []


def main() -> None:
    db_path = Settings().db_path
    print(f"cairn.db: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        conn.enable_load_extension(True)
        conn.load_extension(os.environ.get("SPATIALITE_LIBRARY_PATH", "mod_spatialite"))
        conn.enable_load_extension(False)

        rows = conn.execute(
            f"SELECT id, osm_id, AsGeoJSON(geom) FROM osm_features WHERE {_WHERE} ORDER BY id"
        ).fetchall()
        print(f"MVUM drivable rows: {len(rows)}")

        seen: set[int] = set()      # new stable ids already kept
        rekey: list[tuple] = []     # (new_osm_id, row_id) for survivors needing a change
        delete: list[tuple] = []    # (row_id,) for duplicates
        skipped = 0
        for row_id, cur_osm_id, gj in rows:
            new_id = stable_mvum_id(_geojson_to_paths(gj)) if gj else None
            if new_id is None:
                skipped += 1
                continue
            if new_id in seen:
                delete.append((row_id,))
            else:
                seen.add(new_id)
                if new_id != cur_osm_id:
                    rekey.append((new_id, row_id))

        if skipped:
            print(f"skipped {skipped} rows with no usable geometry")
        if not delete and not rekey:
            print("Already deduped + re-keyed — nothing to do.")
            return

        conn.executemany("DELETE FROM osm_features WHERE id = ?", delete)
        conn.executemany("UPDATE osm_features SET osm_id = ? WHERE id = ?", rekey)
        conn.execute("SELECT RecoverSpatialIndex('osm_features', 'geom')")
        conn.commit()

        after = conn.execute(f"SELECT COUNT(*) FROM osm_features WHERE {_WHERE}").fetchone()[0]
        print(f"Deleted {len(delete)} duplicate rows, re-keyed {len(rekey)} survivors.")
        print(f"MVUM drivable rows now {after}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
