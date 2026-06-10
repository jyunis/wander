"""End-to-end pipeline (UoW-1) with synthetic data — TLC-2/3/6/8/9.

Drives RunService over a real SpatiaLite store with a canned Overpass extract
and a constant-slope fake terrain, then asserts the contract invariants on the
persisted run + its GeoJSON.
"""

from __future__ import annotations

import re

import pytest

from cairn.config import DEFAULT_WEIGHTS
from cairn.service import RunService
from cairn.store import Store
from tests.fakes.pipeline import (
    TEST_BBOX,
    FakeOverpass,
    fake_terrain_factory,
    synthetic_elements,
)


async def _run(store: Store, settings, elements, weights):
    svc = RunService(
        store,
        settings,
        overpass=FakeOverpass(elements),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )
    return await svc.score_bbox(TEST_BBOX, weights)


async def test_full_run_succeeds_and_persists(store, settings):
    run = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    assert run.status == "done"
    assert run.candidate_count > 0
    assert run.failure_reason is None
    # Provenance names both sources with timestamps (TLC-2).
    assert run.provenance["osm"]["source"] == "overpass"
    assert run.provenance["elevation"]["source"] == "terrarium"
    assert run.provenance["osm"]["fetched_at"]


async def test_geojson_carries_disclaimer_and_breakdown(store, settings):
    run = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    gj = await store.get_run_geojson(run.id)
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    assert len(gj["features"]) > 0
    for feat in gj["features"]:
        props = feat["properties"]
        # TLC-8: contributions sum to total.
        s = sum(c["contribution"] for c in props["contributions"])
        assert s == pytest.approx(props["total"], abs=1e-9)
        # ranked + interpretable
        assert "rank" in props and "slope_deg" in props


async def test_tlc3_no_legality_fields_anywhere(store, settings):
    run = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    gj = await store.get_run_geojson(run.id)

    forbidden = re.compile(r"legal|allowed|permitted", re.IGNORECASE)

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert not forbidden.search(str(k)), f"forbidden field name: {k}"
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(gj)


async def test_tlc6_water_layer_absent_degrades_and_renormalizes(store, settings):
    # No water elements, but a positive water weight: water must be reported
    # degraded and excluded with the rest renormalized — never imputed.
    run = await _run(store, settings, synthetic_elements(include_water=False), DEFAULT_WEIGHTS)
    assert run.status == "done"
    assert "water_proximity" in run.degraded_features

    gj = await store.get_run_geojson(run.id)
    for feat in gj["features"]:
        props = feat["properties"]
        features_used = {c["feature"] for c in props["contributions"]}
        assert "water_proximity" not in features_used
        # renormalized weights of the used features sum to 1 (TLC-6).
        wsum = sum(c["weight"] for c in props["contributions"])
        assert wsum == pytest.approx(1.0, abs=1e-9)


async def test_candidates_clipped_to_bbox(store, settings):
    # A track that runs well beyond the bbox on both ends — Overpass returns
    # the whole geometry, but candidates must be clipped to the requested area.
    long_track = {
        "type": "way",
        "id": 999,
        "tags": {"highway": "track"},
        "geometry": [
            {"lon": -0.010, "lat": 0.006},  # west, outside
            {"lon": 0.030, "lat": 0.006},  # east, outside
        ],
    }
    run = await _run(store, settings, [long_track], DEFAULT_WEIGHTS)
    assert run.status == "done"
    assert run.candidate_count > 0
    gj = await store.get_run_geojson(run.id)
    b = TEST_BBOX.normalized()
    for feat in gj["features"]:
        lon, lat = feat["geometry"]["coordinates"]
        assert b.min_lon <= lon <= b.max_lon
        assert b.min_lat <= lat <= b.max_lat


async def test_distinct_site_selection(store, settings):
    run = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    assert run.status == "done"
    # NMS collapses adjacent road samples: distinct sites are fewer than (or
    # equal to) the scored points, and at least one.
    assert 1 <= run.distinct_count <= run.candidate_count

    gj = await store.get_run_geojson(run.id)
    distinct = [f for f in gj["features"] if f["properties"]["distinct"]]
    assert len(distinct) == run.distinct_count
    assert gj["run"]["distinct_count"] == run.distinct_count
    # distinct_rank is dense 1..N over distinct sites, ordered by score.
    ranks = sorted(f["properties"]["distinct_rank"] for f in distinct)
    assert ranks == list(range(1, run.distinct_count + 1))
    # Suppressed candidates carry distinct=False / distinct_rank=None.
    for f in gj["features"]:
        if not f["properties"]["distinct"]:
            assert f["properties"]["distinct_rank"] is None


async def test_tlc9_same_input_reproduces_scores(store, settings):
    r1 = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    r2 = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    assert r1.id != r2.id  # re-scoring is a new run, not a mutation
    g1 = await store.get_run_geojson(r1.id)
    g2 = await store.get_run_geojson(r2.id)
    t1 = [f["properties"]["total"] for f in g1["features"]]
    t2 = [f["properties"]["total"] for f in g2["features"]]
    assert t1 == t2
