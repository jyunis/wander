"""Configuration — the single source for env-supplied settings (FC-7).

Loaded once at boot from `.env` + process environment, `CAIRN_`-prefixed
(except `LOG_LEVEL`, kept bare for parity with the other home-server services).

Carries the caps (TLC-4), default weights, Terrarium zoom, the LRU tile-cache
cap (TLC-13), the ingest chunk size (TLC-14), cache dirs, and the DB path.
The UI-relevant subset is exposed read-only via `GET /config` (FC-8) so the
client never hardcodes weights/presets/caps.

No secrets are required in v1 (TLC-12); a `SecretStr` slot is left for the
day an admin token is added.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default feature weights (sum need not be 1; the scorer renormalizes). Keys
# are the canonical feature names used throughout the engine.
# Tuned 2026-06-11 against the 24 matched drive-in Chippewa ground-truth sites
# (eval/): only road_end (corr +0.33 w/ star rating) and water_proximity (+0.28)
# track real site quality. remoteness (+0.01) and solitude (-0.29) do NOT — they
# were ~30% of the old weight and let generic deep-woods points outrank real
# lakeside spurs, so they're cut hard. road_character stays a strong gate (most
# good sites are on a forest road). tree_cover is 0: OSM has no landcover
# polygons in national forests (only an admin boundary), so it degrades for
# every run anyway — see the eval writeup / would need a landcover raster.
DEFAULT_WEIGHTS: dict[str, float] = {
    "road_end": 0.30,  # spur-end premium ("drive to the end of the road")
    "water_proximity": 0.28,  # ...that ends near a lake — top quality signal
    "road_character": 0.21,  # forest-road class (gate)
    "remoteness": 0.11,  # isolation (low local road density)
    "solitude": 0.05,
    "flatness": 0.05,
    "tree_cover": 0.00,  # unavailable from OSM in national forests; renormalized out
    "trail_proximity": 0.00,  # opt-in; near a mapped hiking trail (preloaded states only)
}

# UI weight presets — each just sets slider values (FC-9). Served via
# GET /config so the client never invents its own. Every preset names all
# features so a switch sets every slider deterministically. (The scorer
# renormalizes, so these need not sum to exactly 1.)
WEIGHT_PRESETS: dict[str, dict[str, float]] = {
    "dispersed": DEFAULT_WEIGHTS,
    "deep_woods": {
        "road_end": 0.25,
        "road_character": 0.20,
        "remoteness": 0.30,
        "water_proximity": 0.05,
        "solitude": 0.15,
        "flatness": 0.05,
        "tree_cover": 0.00,
        "trail_proximity": 0.00,
    },
    "lakeside_spur": {
        "road_end": 0.30,
        "road_character": 0.15,
        "remoteness": 0.10,
        "water_proximity": 0.35,
        "solitude": 0.05,
        "flatness": 0.05,
        "tree_cover": 0.00,
        "trail_proximity": 0.00,
    },
    "flat_and_easy": {
        "road_end": 0.15,
        "road_character": 0.15,
        "remoteness": 0.10,
        "water_proximity": 0.10,
        "solitude": 0.05,
        "flatness": 0.40,
        "tree_cover": 0.05,
        "trail_proximity": 0.00,
    },
}


class Settings(BaseSettings):
    """Process-wide settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CAIRN_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Server ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 9931
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")

    # --- Storage / caches --------------------------------------------------
    data_dir: Path = Path("/data")
    # Resolved relative to data_dir in the validator below if left None.
    db_path: Path | None = None
    tile_cache_dir: Path | None = None
    osm_cache_dir: Path | None = None
    # Static land-overlay DB (display-only ownership + boundaries — TLC-3),
    # produced by scripts/load_overlays.py. Separate from cairn.db, fully
    # re-derivable; the app runs fine if it's absent (no Land layers offered).
    overlays_db_path: Path | None = None
    overlay_max_features: int = 4000  # per /overlays/{layer} response cap

    # --- External services (public, no key — TLC-12) -----------------------
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    terrarium_url: str = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"
    # USFS Motor Vehicle Use Map roads (national forest roads OSM often lacks).
    # Public ArcGIS service, no key. Disable to fall back to OSM-only roads.
    mvum_enabled: bool = True
    mvum_url: str = (
        "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MVUM_01/MapServer/1/query"
    )
    mvum_cache_ttl_s: int = 30 * 24 * 3600
    # FEMA/Oak Ridge USA Structures — national building footprints OSM often
    # lacks (rural cabins). Public ArcGIS service, no key. Unioned with OSM
    # buildings for the no-camping-near-houses setback.
    structures_enabled: bool = True
    structures_url: str = (
        "https://services2.arcgis.com/FiaPA4ga0iQKduv3/ArcGIS/rest/services/"
        "USA_Structures_View/FeatureServer/0/query"
    )
    structures_cache_ttl_s: int = 30 * 24 * 3600
    # Nominatim forward geocoder (OSM, keyless) — turns a place name in the
    # jump-to field into coordinates. Proxied server-side so the request carries
    # Cairn's descriptive User-Agent per the Nominatim usage policy (a browser
    # can't set one). Occasional single-user lookups, well within 1 req/s.
    geocode_enabled: bool = True
    geocode_url: str = "https://nominatim.openstreetmap.org/search"
    geocode_limit: int = 5
    user_agent: str = (
        "cairn/0.1 (self-hosted campsite scorer; +https://cairn.home.arpa)"
    )
    # Live campground availability (Phase 2, display-only — TLC-3). Federal
    # campgrounds via recreation.gov's undocumented-but-stable month endpoint
    # (the same one camply uses). Best-effort + fully degradable: a trip works
    # with no availability — it just shows no open/closed badges (RIDB/MVUM
    # posture). LIVE data -> SHORT TTL only, in the disposable cache (never the
    # long overlay cache). Disable to drop the open/closed annotations.
    #
    # UA exception (TLC-5): unlike every other Cairn courtesy GET, recreation.gov
    # rejects a non-browser User-Agent, so this client sends a browser UA. Still
    # GET-only, no auth, no Authorization header — TLC-12 intact.
    availability_enabled: bool = True
    recgov_availability_url: str = (
        "https://www.recreation.gov/api/camps/availability/campground"
    )
    availability_cache_dir: Path | None = None
    availability_cache_ttl_s: int = 30 * 60  # 30 min — live data, short TTL
    availability_concurrency: int = 2  # polite: rec.gov may rate-limit
    availability_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    # MN state-park availability via UseDirect (Phase 2 / PR2). Same display-only
    # posture + short cache + browser UA (reuses availability_*). The grid is a
    # read exposed as POST. Markers map to a numeric FacilityId via a committed
    # offline-built table (src/cairn/data/usedirect_mn.json, scripts/
    # build_usedirect_map.py) — unmapped markers degrade to link-out.
    usedirect_enabled: bool = True
    usedirect_grid_url: str = "https://mnrdr.usedirect.com/minnesotardr/rdr/search/grid"
    # Recent-precipitation banner for the road-mud feature (P2, display-only —
    # TLC-3; clock-dependent so also TLC-1, never scored). Open-Meteo forecast,
    # keyless, near-real-time. One fetch per forest centroid, cached a few hours
    # in the disposable cache (LIVE data -> short TTL, never the long overlay
    # cache). Disable to drop the wetness banner; the static mud tier is
    # unaffected.
    precip_enabled: bool = True
    precip_url: str = "https://api.open-meteo.com/v1/forecast"
    precip_past_days: int = 7  # daily precip window fetched (<= 92)
    precip_cache_dir: Path | None = None
    precip_cache_ttl_s: int = 6 * 3600  # live-ish; refreshes a few times a day

    # --- Caps (TLC-4) ------------------------------------------------------
    max_bbox_area_km2: float = 2500.0
    max_candidates: int = 5000
    # Wall-clock bounds (TLC-4): a tight bound on the SYNC path (a client is
    # blocked on the HTTP request) and a looser one for ASYNC background jobs
    # ("120 s sync; async beyond") — a large bbox legitimately needs minutes to
    # fetch a multi-MB Overpass extract, decode slope tiles, and score up to
    # max_candidates points, all of which the UI just polls for.
    max_wallclock_seconds: float = 120.0
    max_wallclock_async_seconds: float = 600.0
    # Bboxes at/under this area score synchronously (resolve on the first
    # poll); larger ones run as a background job the UI polls (TLC-4).
    sync_area_threshold_km2: float = 200.0

    # --- Memory invariants (TLC-13 / TLC-14) -------------------------------
    tile_lru_cap: int = 16
    ingest_chunk_size: int = 5000
    # Oversized area geometries (e.g. the full Lake Superior relation, ~219k
    # vertices) are clipped at ingest to the score bbox expanded by this buffer.
    # Every distance feature has a finite search radius (water 600m, forest 200m,
    # solitude/building 3000m), so a buffer >= the largest radius keeps every
    # in-radius distance byte-identical while collapsing a continent-spanning
    # polygon to its local shoreline — turning a per-candidate O(219k-vertex)
    # ST_Distance into O(local). Clipping only kicks in above the vertex
    # threshold, so ordinary features are stored untouched. Purely a perf
    # measure — never changes a computed distance within the search radius.
    ingest_clip_buffer_m: float = 3500.0  # >= max distance-feature search radius
    ingest_clip_vertex_threshold: int = 2000

    # --- Terrain -----------------------------------------------------------
    terrarium_zoom: int = 12

    # --- Candidate generation ---------------------------------------------
    candidate_spacing_m: float = 150.0  # along-way sampling interval (UoW-5)
    candidate_grid_m: float = 100.0  # dedup grid cell size
    # A candidate within this distance of a USFS MVUM forest road gets forest
    # road_character even if its OSM source way is mistagged (OSM↔MVUM reconcile).
    mvum_overlap_m: float = 25.0

    # --- Distinct-site selection (spatial NMS) ----------------------------
    # After scoring, suppress lower-scoring candidates within this radius of a
    # higher-scoring one so ranked "sites" are distinct, not adjacent road
    # samples. Exposed via GET /config so the UI can show/tune it. 150m keeps
    # the "10 pins on one stretch" clutter down while still pinning genuinely
    # separate spots — e.g. two dead-end spurs ~300m apart each keep a pin.
    distinct_radius_m: float = 150.0
    # Candidates within this distance of any OSM building are dropped — no
    # camping in someone's yard. Buildings also feed the solitude gradient.
    building_setback_m: float = 80.0

    # --- External-service courtesy (TLC-5) --------------------------------
    osm_cache_ttl_s: int = 7 * 24 * 3600
    tile_cache_ttl_s: int = 30 * 24 * 3600
    http_concurrency: int = 4
    http_max_retries: int = 3
    overpass_timeout_s: float = 180.0
    tile_timeout_s: float = 30.0

    # --- Secrets (none required in v1) ------------------------------------
    admin_token: SecretStr | None = None

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        if self.db_path is None:
            self.db_path = self.data_dir / "cairn.db"
        if self.tile_cache_dir is None:
            self.tile_cache_dir = self.data_dir / "tiles"
        if self.osm_cache_dir is None:
            self.osm_cache_dir = self.data_dir / "osm-cache"
        if self.overlays_db_path is None:
            self.overlays_db_path = self.data_dir / "overlays.db"
        if self.availability_cache_dir is None:
            # Disposable/derivable (TLC-7), backup-excluded like tiles/osm-cache.
            self.availability_cache_dir = self.data_dir / "availability-cache"
        if self.precip_cache_dir is None:
            self.precip_cache_dir = self.data_dir / "precip-cache"
        return self

    def ui_config(self) -> dict:
        """The read-only subset the UI needs (FC-8 / FC-9)."""
        return {
            "default_weights": DEFAULT_WEIGHTS,
            "presets": WEIGHT_PRESETS,
            "feature_names": list(DEFAULT_WEIGHTS.keys()),
            "max_bbox_area_km2": self.max_bbox_area_km2,
            "sync_area_threshold_km2": self.sync_area_threshold_km2,
            "max_candidates": self.max_candidates,
            "terrarium_zoom": self.terrarium_zoom,
            "distinct_radius_m": self.distinct_radius_m,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings, constructing it once."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
