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
-- v=2: distinct-site selection — candidate_scores.is_distinct/distinct_rank
--      (spatial NMS over the ranked candidates) + runs.distinct_count.
-- v=3: road_character feature — candidate_features.road_score/road_unavailable
--      (forest-road class + roughness from the source way's OSM tags).
-- v=4: remoteness feature (local_road_m/remoteness_unavailable — local road
--      density) + road_rough informational flag.
-- v=5: road_end feature — candidate_features.dist_terminus_m /
--      road_end_unavailable (distance to nearest drivable dead-end).
-- v=6: saved_places — user favorites (dispersed sites + campgrounds).
-- v=7: saved_places.rating — user quality rating, 0-5 stars.
-- v=8: trip planner — trips + trip_members (a dated grouping of saved places).
-- v=9: collections — multi-membership tag groups for organizing saved places
--      (collections + place_collections join). User data, never scoring.

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
    distinct_count          INTEGER NOT NULL DEFAULT 0,   -- distinct sites after NMS
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
    near_campsite           INTEGER NOT NULL DEFAULT 0,   -- enrichment, non-scoring (v1)
    road_score              REAL,                 -- [0,1] forest-road character (v3)
    road_unavailable        INTEGER NOT NULL DEFAULT 0,
    local_road_m            REAL,                 -- local drivable road length (v4, remoteness)
    remoteness_unavailable  INTEGER NOT NULL DEFAULT 0,
    road_rough              INTEGER NOT NULL DEFAULT 0,   -- informational rough/4WD flag (v4)
    dist_terminus_m         REAL,                 -- distance to nearest dead-end (v5, road_end)
    road_end_unavailable    INTEGER NOT NULL DEFAULT 0
);

-- --- Per-candidate scores (UoW-7). Append-only for done runs (TLC-9).
CREATE TABLE IF NOT EXISTS candidate_scores (
    candidate_id        INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    total               REAL NOT NULL,
    contributions_json  TEXT NOT NULL,            -- [{feature, normalized, weight, contribution}] (TLC-8)
    degraded_features_json TEXT NOT NULL DEFAULT '[]',
    is_distinct         INTEGER NOT NULL DEFAULT 1,   -- bool: distinct site after NMS
    distinct_rank       INTEGER                       -- 1-based over distinct sites; NULL if suppressed
);

CREATE INDEX IF NOT EXISTS idx_scores_total ON candidate_scores (total DESC);

-- --- Saved places (favorites) — user data, NOT scoring (v6). A starred
-- dispersed site or campground; payload_json snapshots its detail so the
-- Saved list can re-open it. Plain relational (no geometry ops needed).
CREATE TABLE IF NOT EXISTS saved_places (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,            -- 'site' | 'campground'
    name         TEXT,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    payload_json TEXT,                     -- score breakdown (site) / campground meta
    note         TEXT,
    created_at   TEXT NOT NULL,
    rating       REAL                      -- user quality rating, 0-5 stars (nullable)
);

-- --- Trips (v8) — a named, dated grouping of saved places for one outing.
-- User data + display, never scoring (TLC-3). Flat shortlist, not a sequenced
-- itinerary; `position` is reserved for later sequencing and unused in v1.
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    start_date  TEXT,                      -- ISO 'YYYY-MM-DD', nullable
    end_date    TEXT,                      -- ISO 'YYYY-MM-DD', nullable
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- Each member references a saved_place with a role: 'primary' (a free,
-- first-come dispersed idea) or 'fallback' (a reservable campground). FK
-- cascade prunes members when a trip or the saved place is deleted.
CREATE TABLE IF NOT EXISTS trip_members (
    trip_id    INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    saved_id   INTEGER NOT NULL REFERENCES saved_places(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'primary',   -- 'primary' | 'fallback'
    position   INTEGER NOT NULL DEFAULT 0,        -- reserved for sequencing (v1: unused)
    PRIMARY KEY (trip_id, saved_id)
);

CREATE INDEX IF NOT EXISTS idx_trip_members_trip ON trip_members (trip_id);

-- --- Collections (v9) — named, color-tagged groups for organizing saved
-- places. MULTI-membership (a place can be in several), so a join table.
-- User data + display, never scoring (TLC-3); orthogonal to trips.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT,                              -- UI swatch (e.g. '#5b9c5a')
    position    INTEGER NOT NULL DEFAULT 0,        -- display order
    created_at  TEXT NOT NULL
);

-- A saved place ↔ collection membership. FK cascade prunes memberships when a
-- collection or the saved place is deleted; the place/collection survive.
CREATE TABLE IF NOT EXISTS place_collections (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    saved_id      INTEGER NOT NULL REFERENCES saved_places(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, saved_id)
);

CREATE INDEX IF NOT EXISTS idx_place_collections_saved ON place_collections (saved_id);
