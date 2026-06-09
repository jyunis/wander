"""Feature extraction (FC-4, TLC-6).

One coroutine per candidate produces its scored-feature raw inputs:
  - flatness        <- slope° from the FC-2 bounded LRU sampler (TLC-13)
  - water_proximity <- distance (m) to nearest water
  - tree_cover      <- distance (m) to nearest forest (0 inside a polygon)
  - solitude        <- distance (m) to nearest highway / built-up / place

All spatial computation is issued as parameterized SpatiaLite SQL THROUGH the
store (no raw SQL here, FC-4). A feature whose data layer is absent in the
bbox is marked `unavailable` (TLC-6) — never silently 0. A feature whose layer
is present but with nothing within its useful search radius saturates to that
radius (normalizing to ~0 for water/forest, to max for solitude), which is a
real measurement, not a gap.

This module has no clock/random (TLC-1 purity rule applies to its inputs).
"""

from __future__ import annotations

from cairn.clients.terrain import TerrainSampler
from cairn.engine.types import Candidate, CandidateFeatures, FeatureValue
from cairn.errors import TerrainError
from cairn.store import Store

# Classes that constitute a "disturbance" for solitude.
_SOLITUDE_CLASSES = ("highway", "builtup", "place")
# Within this many meters of a known campsite -> the enrichment flag.
_CAMPSITE_NEAR_M = 250.0


def _search_radii(scoring_config: dict) -> tuple[float, float, float]:
    f = scoring_config["features"]
    water = float(f["water_proximity"].get("fall_end", 1500.0))
    forest = float(f["tree_cover"].get("decay_m", 200.0)) * 7.0  # exp(-7) ~ 0
    solitude = float(f["solitude"].get("cap_m", 3000.0))
    return water, forest, solitude


async def extract_features(
    store: Store,
    terrain: TerrainSampler,
    candidate: Candidate,
    scoring_config: dict,
    present_classes: set[str],
) -> CandidateFeatures:
    lon, lat = candidate.lon, candidate.lat
    water_r, forest_r, solitude_r = _search_radii(scoring_config)

    # flatness <- slope (tile fetch may fail -> unavailable for this candidate)
    try:
        slope = await terrain.slope_deg(lon, lat)
        flatness = FeatureValue(slope, True)
    except TerrainError:
        flatness = FeatureValue(None, False)

    water = await _distance_feature(
        store, lon, lat, ("water",), water_r, "water" in present_classes
    )
    forest = await _distance_feature(
        store, lon, lat, ("forest",), forest_r, "forest" in present_classes
    )
    solitude_present = any(c in present_classes for c in _SOLITUDE_CLASSES)
    solitude = await _distance_feature(
        store, lon, lat, _SOLITUDE_CLASSES, solitude_r, solitude_present
    )

    near_campsite = False
    if "campsite" in present_classes:
        d = await store.nearest_distance_m(lon, lat, ("campsite",), _CAMPSITE_NEAR_M)
        near_campsite = d is not None and d <= _CAMPSITE_NEAR_M

    return CandidateFeatures(
        candidate=candidate,
        features={
            "flatness": flatness,
            "water_proximity": water,
            "tree_cover": forest,
            "solitude": solitude,
        },
        near_campsite=near_campsite,
    )


async def _distance_feature(
    store: Store,
    lon: float,
    lat: float,
    classes: tuple[str, ...],
    radius_m: float,
    layer_present: bool,
) -> FeatureValue:
    """Nearest-distance feature with TLC-6 availability semantics.

    Layer absent -> unavailable. Layer present but nothing within radius ->
    saturate to radius (a real "far" measurement, available)."""
    if not layer_present:
        return FeatureValue(None, False)
    dist = await store.nearest_distance_m(lon, lat, classes, radius_m)
    return FeatureValue(radius_m if dist is None else dist, True)
