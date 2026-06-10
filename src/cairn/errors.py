"""Cairn error taxonomy.

These types are the cross-module contract surface: clients, store, and the
engine raise these specific subclasses so the run executor and API can branch
on them and map to HTTP codes / run failure_reasons. Never let a raw library
exception (httpx, sqlite, PIL) cross a module boundary — wrap it.

See Contracts § FC-1 (Overpass), FC-2 (Terrain), FC-6 (Store), FC-8 (API),
and TLC-10 (failure surfacing).
"""

from __future__ import annotations


class CairnError(Exception):
    """Base for all Cairn-raised errors."""


# --- Configuration / startup ---------------------------------------------


class ConfigError(CairnError):
    """Required config or env var is missing or malformed (FC-7)."""


# --- Request validation (TLC-4) ------------------------------------------


class ValidationError(CairnError):
    """Caller-supplied input is invalid. Maps to HTTP 422."""


class BboxTooLarge(ValidationError):
    """Requested bbox area exceeds the configured cap (TLC-4).

    Carries the offending area and the limit so the API and UI can name the
    bound. No fetch occurs when this is raised.
    """

    def __init__(self, area_km2: float, limit_km2: float) -> None:
        super().__init__(
            f"bbox area {area_km2:.1f} km² exceeds cap of {limit_km2:.0f} km²"
        )
        self.area_km2 = area_km2
        self.limit_km2 = limit_km2


class InvalidBbox(ValidationError):
    """Bbox is malformed (bad ordering, out of range, zero area)."""


class InvalidWeights(ValidationError):
    """Weights are malformed (negative, or no nonzero weight)."""


# --- Overpass client (FC-1) ----------------------------------------------


class OverpassError(CairnError):
    """Base for Overpass client failures."""


class OverpassUnreachable(OverpassError):
    """Could not connect to the Overpass endpoint (network/DNS/refused)."""


class OverpassTimeout(OverpassError):
    """Overpass request exceeded the client timeout."""


class OverpassBadResponse(OverpassError):
    """Overpass returned a non-2xx after retries, or unparseable content."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- MVUM client (USFS Motor Vehicle Use Map roads) ----------------------


class MvumError(CairnError):
    """Base for USFS MVUM road-source failures. Non-fatal: a run degrades to
    OSM-only roads when MVUM is unavailable."""


class MvumUnreachable(MvumError):
    """Could not reach the USFS MVUM ArcGIS service."""


class MvumBadResponse(MvumError):
    """MVUM returned a non-2xx or unparseable/error response."""


# --- USA Structures client (national building footprints) ----------------


class StructuresError(CairnError):
    """Base for USA Structures building-source failures. Non-fatal: a run
    degrades to OSM-only buildings when the service is unavailable."""


class StructuresUnreachable(StructuresError):
    """Could not reach the USA Structures ArcGIS service."""


class StructuresBadResponse(StructuresError):
    """USA Structures returned a non-2xx or unparseable/error response."""


# --- Terrain client (FC-2) -----------------------------------------------


class TerrainError(CairnError):
    """Base for Terrarium tile client failures."""


class TileUnreachable(TerrainError):
    """A Terrarium tile could not be fetched (network or non-2xx)."""


class TileDecodeError(TerrainError):
    """A fetched tile PNG could not be decoded to an elevation array."""


# --- Store (FC-6) --------------------------------------------------------


class StoreError(CairnError):
    """SQLite/SpatiaLite store failure (IO, FK violation, bad SQL)."""


class SpatialiteUnavailable(StoreError):
    """The mod_spatialite extension could not be loaded at connect (FC-6)."""


class SchemaVersionError(StoreError):
    """Store refuses to start against an unrecognized schema version (FC-6)."""


class RunNotFound(StoreError):
    """No run row exists for the requested id (UoW-8 -> HTTP 404)."""


# --- Run execution (TLC-10) ----------------------------------------------


class RunFailed(CairnError):
    """A run ended in a typed failure. `reason` is persisted on the run row
    and surfaced verbatim by the API; never swallowed (TLC-10)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WallClockExceeded(RunFailed):
    """A sync run exceeded the configured wall-clock bound (TLC-4)."""

    def __init__(self, limit_seconds: float) -> None:
        super().__init__(f"wall-clock bound exceeded: {limit_seconds:.0f}s")
        self.limit_seconds = limit_seconds
