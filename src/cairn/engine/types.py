"""Engine value types — the shared contract between candidate generation,
feature extraction, and scoring. Pure data; no I/O, no SQL.

These are the interface the implementer/tester barrier is drawn around
(Contracts § Development protocol): tests are written against these types +
the spec; implementations must satisfy both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical scored feature names. Each maps to one raw input (see
# engine/scoring_config.py: the per-feature "input" field).
FEATURE_NAMES: tuple[str, ...] = (
    "flatness",
    "water_proximity",
    "tree_cover",
    "solitude",
    "road_character",
)


@dataclass(frozen=True)
class Way:
    """A drivable OSM way: an ordered polyline of (lon, lat) vertices."""

    osm_id: int
    coords: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Candidate:
    """A road-anchored candidate point."""

    lon: float
    lat: float
    source_way_id: int


@dataclass(frozen=True)
class FeatureValue:
    """The raw input for one feature at one candidate, plus availability.

    `value` is the RAW measurement the feature normalizes (e.g. slope in
    degrees for flatness, distance-to-water in meters for water_proximity).
    `available=False` means the feature could not be computed here (TLC-6):
    `value` is then meaningless and the feature must be excluded from the
    weighted sum with the remaining weights renormalized — never treated as 0.
    """

    value: float | None
    available: bool = True


@dataclass(frozen=True)
class CandidateFeatures:
    """All scored-feature raw inputs for one candidate.

    `features` is keyed by FEATURE_NAMES; each entry carries the raw input +
    availability. `near_campsite` is enrichment only (non-scoring in v1).
    """

    candidate: Candidate
    features: dict[str, FeatureValue]
    near_campsite: bool = False


@dataclass(frozen=True)
class Contribution:
    """One feature's contribution to a candidate's total (TLC-8).

    `weight` is the RENORMALIZED weight actually used (TLC-6), so that
    sum(contribution) == total across the list.
    """

    feature: str
    normalized: float  # raw input mapped to [0, 1]
    weight: float  # renormalized weight
    contribution: float  # normalized * weight


@dataclass(frozen=True)
class ScoredCandidate:
    """A scored candidate: total + interpretable per-feature breakdown.

    `distinct` / `distinct_rank` are set by the spatial non-maximum
    suppression pass (engine.scoring.select_distinct): a distinct site is a
    local score maximum kept after suppressing lower-scoring neighbours within
    the suppression radius. `distinct_rank` is 1-based over distinct sites
    (None when suppressed). Before selection, candidates default to distinct.
    """

    candidate: Candidate
    total: float
    contributions: list[Contribution]
    degraded_features: list[str] = field(default_factory=list)
    near_campsite: bool = False
    distinct: bool = True
    distinct_rank: int | None = None
