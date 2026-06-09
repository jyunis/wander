-- Cairn schema (SpatiaLite-enabled SQLite).
--
-- Source of truth for runs, candidates, features, scores (TLC-7). Versioned
-- per FC-6; the store refuses to start against an unrecognized version.
--
-- Geometry columns (osm_features.geom, candidates.geom) and their R*Tree
-- spatial indexes are NOT created here — they require InitSpatialMetaData to
-- have run first and are added via AddGeometryColumn / CreateSpatialIndex by
-- store/repo.py after this script executes. Everything here is plain DDL so
-- it is safe to run through executescript() and idempotent (IF NOT EXISTS).
--
-- v=1: initial schema.

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- --- Scoring config (TLC-1 / FC-5): normalization shapes + hard filters as
-- --- DATA, not scattered constants. One immutable JSON blob per version;
-- --- runs reference the version they used for provenance (TLC-2).
CREATE TABLE IF NOT EXISTS scoring_config (
    version     INTEGER PRIMARY KEY,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- --- Runs: one row per score_bbox invocation. Immutable once done (TLC-9).
CREATE TABLE IF NOT EXISTS runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    -- normalized bbox (WGS84 degrees)
    min_lon                 REAL NOT NULL,
    min_lat                 REAL NOT NULL,
    max_lon                 REAL NOT NULL,
    max_lat                 REAL NOT NULL,
    weights_json            TEXT NOT NULL,
    scoring_config_version  INTEGER NOT NULL REFERENCES scoring_config(version),
    provenance_json         TEXT,                 -- written before status=done (TLC-2)
    status                  TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','running','done','failed')),
    failure_reason          TEXT,                 -- typed reason when failed (TLC-10)
    candidate_count         INTEGER NOT NULL DEFAULT 0,
    capped                  INTEGER NOT NULL DEFAULT 0,   -- bool: MAX_CANDIDATES hit (TLC-4)
    degraded_features_json  TEXT,                 -- run-level degraded list (TLC-6)
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    done_at                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);

-- --- OSM features: ingested, idempotent-keyed by (osm_type, osm_id) (TLC-11).
-- --- geom column added separately (generic GEOMETRY: pt/line/poly).
CREATE TABLE IF NOT EXISTS osm_features (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    osm_type        TEXT NOT NULL,                -- 'node' | 'way' | 'relation'
    osm_id          INTEGER NOT NULL,
    feature_class   TEXT NOT NULL,                -- 'drivable' | 'water' | 'forest' | 'highway' | 'builtup' | 'place' | 'campsite'
    tags_json       TEXT NOT NULL DEFAULT '{}',
    UNIQUE (osm_type, osm_id, feature_class)
);

CREATE INDEX IF NOT EXISTS idx_osm_class ON osm_features (feature_class);

-- --- Candidates: road-anchored points for a run (UoW-5). geom POINT.
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_way_id   INTEGER,
    capped          INTEGER NOT NULL DEFAULT 0,
    lon             REAL NOT NULL,                -- denormalized for cheap GeoJSON emit
    lat             REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates (run_id);

-- --- Per-candidate features (UoW-6). Each feature has a value + availability
-- --- flag; an unavailable feature is excluded + weights renormalized (TLC-6).
CREATE TABLE IF NOT EXISTS candidate_features (
    candidate_id            INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    slope_deg               REAL,
    slope_unavailable       INTEGER NOT NULL DEFAULT 0,
    dist_water_m            REAL,
    water_unavailable       INTEGER NOT NULL DEFAULT 0,
    dist_forest_m           REAL,
    forest_unavailable      INTEGER NOT NULL DEFAULT 0,
    dist_solitude_m         REAL,                 -- nearest highway/builtup/place
    solitude_unavailable    INTEGER NOT NULL DEFAULT 0,
    near_campsite           INTEGER NOT NULL DEFAULT 0   -- enrichment, non-scoring (v1)
);

-- --- Per-candidate scores (UoW-7). Append-only for done runs (TLC-9).
CREATE TABLE IF NOT EXISTS candidate_scores (
    candidate_id        INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    total               REAL NOT NULL,
    contributions_json  TEXT NOT NULL,            -- [{feature, normalized, weight, contribution}] (TLC-8)
    degraded_features_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_scores_total ON candidate_scores (total DESC);
