"""Scoring core (FC-5, TLC-1, TLC-6, TLC-8).

`score(candidates, weights, scoring_config) -> list[ScoredCandidate]` is a
PURE, deterministic function: given identical inputs it produces byte-identical
ranked output — same scores, same order, same tie-breaking. No clock, no
randomness, no PID, no environment reads (TLC-1, enforced by
tests/unit/test_tlc1_purity.py).

Normalization shapes and the hard-filter threshold are DATA read from
`scoring_config` (engine/scoring_config.py defines the v1 seed), never
constants scattered here. Weights are renormalized over the available features
(TLC-6); the total decomposes into per-feature contributions that sum to it
(TLC-8).

INTERFACE ONLY — implemented under the tester/implementer barrier.
"""

from __future__ import annotations

import math
from dataclasses import replace

from cairn.engine.types import (
    Candidate,
    CandidateFeatures,
    Contribution,
    ScoredCandidate,
)

_M_PER_DEG = 111320.0


def _normalize(shape: str, value: float, params: dict) -> float:
    """Map a raw feature input to [0, 1] using its configured shape.

    Shape params are read from `params` (the per-feature scoring_config entry),
    never hardcoded. Adding a shape later is a local change here.
    """
    if shape == "linear_decreasing":
        soft = params["soft_deg"]
        if soft <= 0.0:
            return 0.0
        return _clamp01(1.0 - value / soft)
    if shape == "linear_increasing":
        cap = params["cap_m"]
        if cap <= 0.0:
            return 1.0
        return min(value / cap, 1.0)
    if shape == "distance_decay":
        decay = params["decay_m"]
        if decay <= 0.0:
            return 0.0
        return math.exp(-value / decay)
    if shape == "trapezoid":
        return _trapezoid(value, params)
    if shape == "passthrough":
        # value is already normalized to [0,1] by the feature extractor.
        return _clamp01(value)
    if shape == "linear_falloff":
        # 1 at value 0, linearly down to 0 at `zero_at`. Like
        # linear_decreasing but with a generic (non-slope) param name; used for
        # remoteness over a road-density range that doesn't start at 0.
        zero_at = params["zero_at"]
        if zero_at <= 0.0:
            return 0.0
        return _clamp01(1.0 - value / zero_at)
    raise ValueError(f"unknown normalization shape: {shape!r}")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _trapezoid(d: float, params: dict) -> float:
    rise_start = params["rise_start"]
    plateau_lo = params["plateau_lo"]
    plateau_hi = params["plateau_hi"]
    fall_end = params["fall_end"]
    if d < rise_start or d >= fall_end:
        return 0.0
    if d < plateau_lo:
        width = plateau_lo - rise_start
        if width <= 0.0:
            return 1.0
        return (d - rise_start) / width
    if d <= plateau_hi:
        return 1.0
    width = fall_end - plateau_hi
    if width <= 0.0:
        return 0.0
    return (fall_end - d) / width


def score(
    candidates: list[CandidateFeatures],
    weights: dict[str, float],
    scoring_config: dict,
) -> list[ScoredCandidate]:
    """Score and rank candidates.

    Semantics (from the Contracts):
      - Hard filter (TLC-4): a candidate whose flatness input (slope°) is
        available AND exceeds the config's hard threshold is DROPPED entirely
        (absent from the output), not scored 0.
      - Per available feature with weight > 0, map the raw input to [0, 1] via
        the feature's configured shape.
      - Renormalize the weights over the features that are both weight > 0 and
        available (TLC-6); an unavailable weighted feature is recorded in
        `degraded_features` and excluded from the sum (never treated as 0).
      - total = Σ normalizedᵢ · renormalized_weightᵢ; contributions sum to
        total within float epsilon (TLC-8).
      - Deterministic order: total descending, with a stable, specified
        tie-break (TLC-1).

    Pure: no I/O, no clock, no randomness.
    """
    feature_cfg = scoring_config["features"]
    flat_cfg = feature_cfg.get("flatness")
    hard_deg = flat_cfg.get("hard_deg") if flat_cfg else None

    # Feature names with positive weight, in a deterministic order (the config's).
    weighted = [name for name in feature_cfg if weights.get(name, 0.0) > 0.0]

    scored: list[ScoredCandidate] = []

    for cf in candidates:
        feats = cf.features

        # Hard filter: pre-disqualified candidates (e.g. within the building
        # setback) are dropped entirely (TLC-4).
        if cf.disqualified:
            continue

        # Hard filter: drop only when slope is available AND strictly above hard.
        if hard_deg is not None and "flatness" in feats:
            fv = feats["flatness"]
            if fv.available and fv.value is not None and fv.value > hard_deg:
                continue

        contributions: list[Contribution] = []
        degraded: list[str] = []
        normalized_by_feature: dict[str, float] = {}
        avail_weight_sum = 0.0

        for name in weighted:
            fv = feats.get(name)
            if fv is None or not fv.available or fv.value is None:
                degraded.append(name)
                continue
            cfg = feature_cfg[name]
            normalized_by_feature[name] = _normalize(cfg["shape"], fv.value, cfg)
            avail_weight_sum += weights[name]

        if avail_weight_sum <= 0.0:
            total = 0.0
            contributions = []
        else:
            total = 0.0
            for name in weighted:
                if name not in normalized_by_feature:
                    continue
                norm = normalized_by_feature[name]
                renorm_w = weights[name] / avail_weight_sum
                contrib = norm * renorm_w
                total += contrib
                contributions.append(
                    Contribution(
                        feature=name,
                        normalized=norm,
                        weight=renorm_w,
                        contribution=contrib,
                    )
                )

        scored.append(
            ScoredCandidate(
                candidate=cf.candidate,
                total=total,
                contributions=contributions,
                degraded_features=degraded,
                near_campsite=cf.near_campsite,
            )
        )

    # Sort: total descending; stable deterministic tie-break on coordinates +
    # source way id (NOT builtin hash, which is salted across processes).
    scored.sort(
        key=lambda sc: (
            -sc.total,
            sc.candidate.lon,
            sc.candidate.lat,
            sc.candidate.source_way_id,
        )
    )
    return scored


def _distance_m(a: Candidate, b: Candidate) -> float:
    """Equirectangular distance in meters between two candidate points. Pure."""
    mean_lat = math.radians((a.lat + b.lat) / 2.0)
    dx = (a.lon - b.lon) * _M_PER_DEG * math.cos(mean_lat)
    dy = (a.lat - b.lat) * _M_PER_DEG
    return math.hypot(dx, dy)


def select_distinct(
    scored: list[ScoredCandidate], radius_m: float
) -> list[ScoredCandidate]:
    """Greedy spatial non-maximum suppression over already-ranked candidates.

    `scored` must be in descending score order (as `score()` returns it).
    Walking highest-first, a candidate is a *distinct site* if it is at least
    `radius_m` from every higher-scoring distinct site already kept; otherwise
    it is suppressed (represented by that nearby better site). Returns a new
    list, same order, with `distinct` / `distinct_rank` set (rank is 1-based
    over distinct sites; None when suppressed).

    Pure + deterministic given the (deterministic) input order. `radius_m <= 0`
    keeps every candidate distinct (no suppression).
    """
    kept: list[ScoredCandidate] = []
    out: list[ScoredCandidate] = []
    next_rank = 1
    for sc in scored:
        is_distinct = True
        if radius_m > 0.0:
            for k in kept:
                if _distance_m(sc.candidate, k.candidate) < radius_m:
                    is_distinct = False
                    break
        if is_distinct:
            kept.append(sc)
            out.append(replace(sc, distinct=True, distinct_rank=next_rank))
            next_rank += 1
        else:
            out.append(replace(sc, distinct=False, distinct_rank=None))
    return out
