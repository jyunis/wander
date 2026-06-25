"""Scoring configuration — normalization shapes + hard filters as DATA (FC-5).

The scorer reads these parameters from the `scoring_config` DB table; this
module holds the bootstrap seed (version 1) the store inserts on init. Runs
record which `scoring_config_version` they used (TLC-2), so changing these
defaults later means seeding a new version, never mutating an old one (TLC-9).

These v1 numbers are a deliberate starting point (flagged to the owner), NOT
spec-pinned constants — they are tunable knobs. The only spec-given value is
the hard slope filter, SLOPE_HARD = 15° (Architecture § Feature set).

Normalization conventions (every feature maps a raw value -> [0, 1]):
  - flatness        linear_decreasing on slope°: 1 - slope/soft_deg, clamped
                    [0,1]; candidates with slope > hard_deg are hard-dropped
                    before scoring (not merely scored 0).
  - water_proximity trapezoid band on distance-to-water (m): rises 0->1 over
                    [rise_start, plateau_lo], holds 1 over the plateau, falls
                    1->0 over [plateau_hi, fall_end]. Peaked, not monotonic.
  - tree_cover      exponential distance-decay on distance-to-forest (m):
                    exp(-d / decay_m). 1 inside/at a forest edge, fading out.
  - solitude        linear_increasing on distance-to-nearest
                    highway/built-up/place (m): min(d / cap_m, 1).
"""

from __future__ import annotations

SCORING_CONFIG_VERSION = 7  # +trail_proximity (display-only trails -> a scoring lens)

DEFAULT_SCORING_PARAMS: dict = {
    "version": SCORING_CONFIG_VERSION,
    "features": {
        "flatness": {
            "input": "slope_deg",
            "shape": "linear_decreasing",
            "soft_deg": 20.0,
            "hard_deg": 15.0,  # SLOPE_HARD — hard filter (TLC-4)
        },
        "water_proximity": {
            "input": "dist_water_m",
            # On-the-water is GOOD: dispersed lakeside / boat-landing sites sit
            # right at the shore. Only a tiny rising edge (0-10 m) avoids
            # literally-in-the-water points; 10-150 m is full marks; fall-off to
            # 600 m so "leads to a lake" means actually near water. (Earlier the
            # rise went to 30 m and dinged true shoreline sites — a boat landing
            # 14 m from the water lost to a point 130 m inland.)
            "shape": "trapezoid",
            "rise_start": 0.0,
            "plateau_lo": 10.0,
            "plateau_hi": 150.0,
            "fall_end": 600.0,
        },
        "tree_cover": {
            "input": "dist_forest_m",
            "shape": "distance_decay",
            "decay_m": 200.0,
        },
        "solitude": {
            "input": "dist_solitude_m",
            "shape": "linear_increasing",
            "cap_m": 3000.0,
        },
        # road_character — how "dispersed-camping-ish" the candidate's source
        # road is. Computed in features.py from the source way's OSM tags using
        # the data below; already in [0,1], so the scorer passes it through.
        # Dominant signal (ground truth: 18/25 drive-in sites on Forest Roads).
        "road_character": {
            "input": "road_score",
            "shape": "passthrough",
            # base score by highway class (candidates only come from drivable
            # classes: track / unclassified / service / residential / tertiary).
            "class_scores": {
                "track": 1.0,
                "unclassified": 0.75,
                "service": 0.5,
                "residential": 0.25,
                "tertiary": 0.15,
                "_default": 0.3,
            },
            # A road named/operated as a Forest Service road counts at least
            # this much even if its class is humbler (service/unclassified FRs).
            "forest_named_score": 1.0,
            # NOTE: roughness no longer penalizes the score — 4WD + decent
            # clearance means rough access should INCREASE hits, not cost them.
            # It is surfaced as an informational `rough` flag instead (FC-4).
        },
        # remoteness <- inverse local road density. Few/short drivable roads
        # within `radius_m` of the candidate => more isolated. exp(-L/decay)
        # over road-meters L: L=0 -> 1 (no roads nearby), L=decay -> ~0.37.
        # This is the signal that separates a quiet dead-end spur from a forest
        # road next to a busy boat launch (both score road_character ~1.0).
        "remoteness": {
            "input": "local_road_m",
            # Linear fall-off (not exp): with the USFS MVUM roads added, local
            # road density sits in a tight high band (~8-23 km within 1.5 km),
            # so exp(-L/2500) saturated to ~0 for everyone. Linear 1->0 over
            # [0, zero_at] spreads that band (lower density = more isolated).
            "shape": "linear_falloff",
            "zero_at": 24000.0,
            "radius_m": 1500.0,
        },
        # road_end <- distance to the nearest drivable DEAD-END (a road that
        # branches off and terminates). The classic dispersed pattern is "drive
        # the spur to where it ends." exp(-d/decay): at a dead-end -> 1, far up
        # a through-road -> ~0. Unavailable only if the bbox has no dead-ends.
        "road_end": {
            "input": "dist_terminus_m",
            "shape": "distance_decay",
            "decay_m": 400.0,
            # An endpoint is a terminus if no OTHER drivable way has a vertex
            # within this many meters (computed on UNCLIPPED geometry).
            "terminus_tol_m": 20.0,
        },
        # trail_proximity <- distance to the nearest mapped hiking trail (the
        # display-only Trails overlay's data, feature_class='trail'). Closer is
        # better (trail access from a dispersed site). distance_decay: at the
        # trail -> 1, ~200 m -> 0.37, ~600 m -> 0.05. OFF by default (weight 0 in
        # DEFAULT_WEIGHTS) — opt-in via the slider. Only has data where trails
        # are PRELOADED (currently MN/WI/MI/SD); degraded + renormalized away
        # elsewhere (TLC-6). Suitability, never a legality claim (TLC-3).
        "trail_proximity": {
            "input": "dist_trail_m",
            "shape": "distance_decay",
            "decay_m": 200.0,
        },
    },
}

# Road tags that warrant a "rough / high-clearance" flag (informational only;
# does NOT reduce the score). Ordinary unpaved/gravel/dirt is normal for forest
# roads and is NOT flagged.
ROUGH_TRACKTYPES = {"grade4", "grade5"}
ROUGH_SMOOTHNESS = {"bad", "very_bad", "horrible", "very_horrible", "impassable"}
