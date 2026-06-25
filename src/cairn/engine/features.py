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

import math
import re

from cairn.clients.terrain import TerrainSampler
from cairn.engine.scoring_config import ROUGH_SMOOTHNESS, ROUGH_TRACKTYPES
from cairn.engine.types import Candidate, CandidateFeatures, FeatureValue
from cairn.errors import TerrainError
from cairn.store import Store

# Classes that constitute a "disturbance" for solitude (now incl. individual
# buildings — a lone cabin matters, not just residential zones).
_SOLITUDE_CLASSES = ("highway", "builtup", "place", "building")
# Within this many meters of a known campsite -> the enrichment flag.
_CAMPSITE_NEAR_M = 250.0
# Search radius for the building-setback hard filter.
_BUILDING_SEARCH_M = 400.0

# Name/ref/operator patterns that mark a road as a Forest Service road.
_FOREST_ROAD_RE = re.compile(
    r"forest\s*(road|rd|service|route)|national\s*forest|\bf\.?\s?r\.?\s*\d|\bfs[rh]?\s*\d",
    re.IGNORECASE,
)


def compute_road_score(tags: dict, cfg: dict, forest_overlap: bool = False) -> float:
    """Map a source way's OSM tags to a [0,1] 'dispersed-camping-ish' score.

    Pure (TLC-1). Driven by the scoring-config `road_character` block: base
    score by highway class + a floor for Forest-Service-named roads. Roughness
    is NOT penalized here (it's a separate informational flag) — a rough forest
    track is a full hit.

    `forest_overlap` floors the score the same way a Forest-Service name does:
    the caller sets it when a USFS MVUM forest road runs right alongside this
    point, which means the source way is the OSM copy of a forest road that OSM
    happens to mistag (e.g. highway=residential) — see the MVUM↔OSM reconcile.
    """
    classes = cfg["class_scores"]
    base = classes.get(tags.get("highway"), classes["_default"])
    name = f"{tags.get('name', '')} {tags.get('ref', '')}"
    operator = tags.get("operator", "").lower()
    if forest_overlap or _FOREST_ROAD_RE.search(name) or "forest service" in operator:
        base = max(base, cfg.get("forest_named_score", 1.0))
    return max(0.0, min(1.0, base))


def road_is_rough(tags: dict) -> bool:
    """Whether a road warrants a 'rough / high-clearance' flag. Informational
    only — does not affect the score. Ordinary unpaved/gravel/dirt is normal
    for a forest road and is NOT flagged."""
    return (
        tags.get("4wd_only") == "yes"
        or tags.get("tracktype") in ROUGH_TRACKTYPES
        or tags.get("smoothness") in ROUGH_SMOOTHNESS
    )


def _search_radii(scoring_config: dict) -> tuple[float, float, float, float]:
    f = scoring_config["features"]
    water = float(f["water_proximity"].get("fall_end", 1500.0))
    forest = float(f["tree_cover"].get("decay_m", 200.0)) * 7.0  # exp(-7) ~ 0
    solitude = float(f["solitude"].get("cap_m", 3000.0))
    trail = float(f.get("trail_proximity", {}).get("decay_m", 200.0)) * 7.0  # exp(-7) ~ 0
    return water, forest, solitude, trail


def _dist_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * 111320.0 * math.cos(mean_lat)
    dy = (lat2 - lat1) * 111320.0
    return math.hypot(dx, dy)


async def extract_features(
    store: Store,
    terrain: TerrainSampler,
    candidate: Candidate,
    scoring_config: dict,
    present_classes: set[str],
    road_tags: dict[int, dict],
    termini: list[tuple[float, float, int]],
    building_setback_m: float,
    forest_overlap: bool = False,
) -> CandidateFeatures:
    lon, lat = candidate.lon, candidate.lat
    water_r, forest_r, solitude_r, trail_r = _search_radii(scoring_config)

    # Building setback (hard filter, TLC-4): no camping in someone's yard.
    disqualified = False
    if "building" in present_classes:
        db = await store.nearest_distance_m(lon, lat, ("building",), _BUILDING_SEARCH_M)
        if db is not None and db < building_setback_m:
            disqualified = True

    # road_end <- distance to the nearest drivable dead-end. Unavailable (and
    # renormalized away, TLC-6) when the bbox has no termini at all.
    if termini:
        dist_terminus = min(_dist_m(lon, lat, tl, ta) for tl, ta, _ in termini)
        road_end = FeatureValue(dist_terminus, True)
    else:
        road_end = FeatureValue(None, False)

    # road_character <- the candidate's source way tags (always available when
    # the source road resolved; degraded only if its tags went missing).
    # `forest_overlap` floors it to forest-road class when a USFS MVUM forest
    # road runs alongside (the OSM source is the mistagged copy — TLC reconcile).
    tags = road_tags.get(candidate.source_way_id)
    rough = False
    if tags is None and not forest_overlap:
        road = FeatureValue(None, False)
    else:
        rcfg = scoring_config["features"]["road_character"]
        road = FeatureValue(compute_road_score(tags or {}, rcfg, forest_overlap), True)
        rough = road_is_rough(tags or {})

    # remoteness <- inverse local drivable-road density within radius_m.
    rcfg = scoring_config["features"]["remoteness"]
    local_m = await store.local_road_length_m(lon, lat, rcfg["radius_m"])
    remoteness = FeatureValue(local_m, True)

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
    # trail_proximity <- nearest mapped hiking trail (preloaded 'trail' overlay
    # data). Unavailable (degraded, TLC-6) where no trails are loaded.
    trail = await _distance_feature(
        store, lon, lat, ("trail",), trail_r, "trail" in present_classes
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
            "road_character": road,
            "remoteness": remoteness,
            "road_end": road_end,
            "trail_proximity": trail,
        },
        near_campsite=near_campsite,
        rough_road=rough,
        disqualified=disqualified,
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
