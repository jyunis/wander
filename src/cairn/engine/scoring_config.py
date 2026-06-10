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

SCORING_CONFIG_VERSION = 2

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
            "shape": "trapezoid",
            "rise_start": 0.0,
            "plateau_lo": 60.0,
            "plateau_hi": 150.0,
            "fall_end": 1500.0,
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
            # Roughness: the tow vehicle has 4WD + decent teardrop clearance, so this is
            # a MILD nudge only — ordinary unpaved/gravel/dirt (the target forest
            # roads) get NO penalty; only genuinely rough/impassable is dinged.
            "rough": {
                "fourwd_factor": 0.9,  # 4wd_only=yes or tracktype=grade5
                "impassable_factor": 0.8,  # smoothness very_bad/horrible/impassable
            },
        },
    },
}
