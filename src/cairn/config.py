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
# are the canonical feature names used throughout the engine. flatness is the
# highest-signal feature (Architecture § Feature set).
# Defaults lean toward forest-road dispersed camping: road_character is the
# dominant signal (ground truth: 18/25 drive-in sites on Forest Roads), with
# solitude second. flatness/water/trees discriminate little in flat lake
# country, so they're secondary.
DEFAULT_WEIGHTS: dict[str, float] = {
    "road_character": 0.30,
    "remoteness": 0.30,
    "solitude": 0.15,
    "flatness": 0.10,
    "water_proximity": 0.10,
    "tree_cover": 0.05,
}

# UI weight presets — each just sets slider values (FC-9). Served via
# GET /config so the client never invents its own. Every preset names all
# features so a switch sets every slider deterministically.
WEIGHT_PRESETS: dict[str, dict[str, float]] = {
    "dispersed": DEFAULT_WEIGHTS,
    "deep_woods": {
        "road_character": 0.30,
        "remoteness": 0.45,
        "solitude": 0.15,
        "flatness": 0.05,
        "water_proximity": 0.05,
        "tree_cover": 0.00,
    },
    "lakeside": {
        "road_character": 0.25,
        "remoteness": 0.15,
        "solitude": 0.10,
        "flatness": 0.10,
        "water_proximity": 0.35,
        "tree_cover": 0.05,
    },
    "flat_and_easy": {
        "road_character": 0.20,
        "remoteness": 0.15,
        "solitude": 0.10,
        "flatness": 0.45,
        "water_proximity": 0.10,
        "tree_cover": 0.00,
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

    # --- External services (public, no key — TLC-12) -----------------------
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    terrarium_url: str = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"
    user_agent: str = (
        "cairn/0.1 (self-hosted campsite scorer; +https://cairn.home.arpa)"
    )

    # --- Caps (TLC-4) ------------------------------------------------------
    max_bbox_area_km2: float = 2500.0
    max_candidates: int = 5000
    max_wallclock_seconds: float = 120.0
    # Bboxes at/under this area score synchronously (resolve on the first
    # poll); larger ones run as a background job the UI polls (TLC-4).
    sync_area_threshold_km2: float = 200.0

    # --- Memory invariants (TLC-13 / TLC-14) -------------------------------
    tile_lru_cap: int = 16
    ingest_chunk_size: int = 5000

    # --- Terrain -----------------------------------------------------------
    terrarium_zoom: int = 12

    # --- Candidate generation ---------------------------------------------
    candidate_spacing_m: float = 150.0  # along-way sampling interval (UoW-5)
    candidate_grid_m: float = 100.0  # dedup grid cell size

    # --- Distinct-site selection (spatial NMS) ----------------------------
    # After scoring, suppress lower-scoring candidates within this radius of a
    # higher-scoring one so ranked "sites" are distinct, not adjacent road
    # samples. Exposed via GET /config so the UI can show/tune it.
    distinct_radius_m: float = 400.0

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
        return self

    def ui_config(self) -> dict:
        """The read-only subset the UI needs (FC-8 / FC-9)."""
        return {
            "default_weights": DEFAULT_WEIGHTS,
            "presets": WEIGHT_PRESETS,
            "feature_names": list(DEFAULT_WEIGHTS.keys()),
            "max_bbox_area_km2": self.max_bbox_area_km2,
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
