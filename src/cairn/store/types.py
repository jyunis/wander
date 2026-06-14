"""Store row dataclasses + the Bbox value type.

Canonical definition site for the typed rows the repository returns. Kept
free of SQL so they can be imported by the engine and API without dragging in
aiosqlite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["pending", "running", "done", "failed"]


@dataclass(frozen=True)
class Bbox:
    """A WGS84 bounding box in degrees. Normalized = min < max on each axis."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def normalized(self) -> "Bbox":
        return Bbox(
            min(self.min_lon, self.max_lon),
            min(self.min_lat, self.max_lat),
            max(self.min_lon, self.max_lon),
            max(self.min_lat, self.max_lat),
        )

    def area_km2(self) -> float:
        """Approximate area in km² (equirectangular, adequate for the cap)."""
        import math

        mid_lat = math.radians((self.min_lat + self.max_lat) / 2.0)
        km_per_deg_lat = 111.320
        km_per_deg_lon = 111.320 * math.cos(mid_lat)
        h = (self.max_lat - self.min_lat) * km_per_deg_lat
        w = (self.max_lon - self.min_lon) * km_per_deg_lon
        return abs(w * h)


@dataclass
class Run:
    """A `runs` row."""

    id: int
    bbox: Bbox
    weights: dict[str, float]
    scoring_config_version: int
    status: RunStatus
    provenance: dict | None = None
    failure_reason: str | None = None
    candidate_count: int = 0
    distinct_count: int = 0
    capped: bool = False
    degraded_features: list[str] = field(default_factory=list)
    created_at: str | None = None
    done_at: str | None = None
    place: str | None = None  # reverse-geocoded bbox-center label (display-only)
