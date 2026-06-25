"""End-to-end pipeline (UoW-1) with synthetic data — TLC-2/3/6/8/9.

Drives RunService over a real SpatiaLite store with a canned Overpass extract
and a constant-slope fake terrain, then asserts the contract invariants on the
persisted run + its GeoJSON.
"""

from __future__ import annotations

import re

import pytest

from cairn.config import DEFAULT_WEIGHTS
from cairn.errors import BboxTooLarge, OverpassUnreachable, RunFailed
from cairn.service import SCORING_CONFIG_VERSION, RunService, validate_request
from cairn.store import Store
from cairn.store.types import Bbox
from tests.fakes.pipeline import (
    TEST_BBOX,
    FakeOverpass,
    fake_terrain_factory,
    synthetic_elements,
)


# --- FC-17 / TLC-15 / TLC-18: roving "Score this view" (ephemeral) ---------
# (`_svc(store, settings)` helper defined below, near the wall-clock test.)
async def test_score_view_returns_geojson_and_persists_no_run(store, settings):
    svc = _svc(store, settings)
    before = len(await store.list_runs())
    gj = await svc.score_view(TEST_BBOX, DEFAULT_WEIGHTS)
    # Returns the same ranked FeatureCollection shape POST /score's geojson does.
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) > 0
    assert "physical suitability only" in gj["disclaimer"].lower()
    # TLC-15: the scratch run is deleted — nothing persists.
    assert len(await store.list_runs()) == before


async def test_score_view_over_sync_cap_raises(store, settings):
    svc = _svc(store, settings)
    # ~296 km² at ~0°: over the 200 km² sync cap, under the 2500 km² hard cap.
    big = Bbox(0.0, 0.0, 0.2, 0.12)
    assert big.area_km2() > settings.sync_area_threshold_km2
    assert big.area_km2() < settings.max_bbox_area_km2
    with pytest.raises(BboxTooLarge):
        await svc.score_view(big, DEFAULT_WEIGHTS)
    # Raised before any work — no scratch run left behind.
    assert len(await store.list_runs()) == 0


class _RaisingOverpass:
    """Overpass that always fails (e.g. the service is unreachable)."""

    async def fetch(self, bbox):
        raise OverpassUnreachable("overpass-api.de refused the connection")

    async def aclose(self):
        pass


async def test_score_view_failed_run_raises_not_empty(store, settings):
    # A run that fails internally (Overpass down, wall-clock, …) must SURFACE the
    # failure on the sync path — not return an empty FeatureCollection that looks
    # identical to a genuinely empty area ("finished, no results, no error").
    svc = RunService(
        store,
        settings,
        overpass=_RaisingOverpass(),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )
    with pytest.raises(RunFailed) as ei:
        await svc.score_view(TEST_BBOX, DEFAULT_WEIGHTS)
    assert "Overpass" in str(ei.value)
    # The scratch run is still cleaned up (deleted in the finally).
    assert len(await store.list_runs()) == 0


async def test_score_view_clip_restricts_candidates(store, settings):
    svc = _svc(store, settings)
    wkb = await _wkb_polygon(store, "POLYGON((0.010 0,0.02 0,0.02 0.012,0.010 0.012,0.010 0))")
    gj = await svc.score_view(TEST_BBOX, DEFAULT_WEIGHTS, clip_polygons=[wkb])
    for feat in gj["features"]:
        lon = feat["geometry"]["coordinates"][0]
        assert lon >= 0.010 - 1e-9, f"candidate at lon {lon} leaked outside the clip"
    assert len(await store.list_runs()) == 0  # still ephemeral


# Persist + execute a run through the live engine primitives (create_run →
# _execute → get_run) — exactly what the retired score_bbox did internally. The
# persisted-run *backend* is gone (doc #34 #4), but these primitives remain (the
# ephemeral /score/view path uses them), so the engine contracts below still test
# live code; we just need a durable run to inspect.
async def _persist(svc, store, settings, weights, bbox=TEST_BBOX, clip_polygons=None):
    b = validate_request(bbox, weights, settings)
    params = await store.scoring_config_params(SCORING_CONFIG_VERSION)
    run_id = await store.create_run(b, weights, SCORING_CONFIG_VERSION)
    await svc._execute(run_id, b, weights, params, settings.max_wallclock_seconds, clip_polygons)
    return await store.get_run(run_id)


async def _run(store: Store, settings, elements, weights):
    svc = RunService(
        store,
        settings,
        overpass=FakeOverpass(elements),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )
    return await _persist(svc, store, settings, weights)


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


async def _wkb_polygon(store, wkt: str) -> bytes:
    cur = await store.db.execute("SELECT ST_AsBinary(GeomFromText(?, 4326))", (wkt,))
    return bytes((await cur.fetchone())[0])


async def test_points_within_polygon(store):
    wkb = await _wkb_polygon(store, "POLYGON((0 0,2 0,2 2,0 2,0 0))")
    # inside, outside, inside
    mask = await store.points_within_polygon([(1.0, 1.0), (3.0, 3.0), (0.5, 0.5)], wkb)
    assert mask == [True, False, True]
    assert await store.points_within_polygon([], wkb) == []


async def test_clip_filters_candidates_to_boundary(store, settings):
    # The synthetic track runs lon 0.002..0.018 at lat 0.010. Clip to the eastern
    # half (lon >= 0.010): only candidates inside the polygon survive, scoring is
    # unchanged for those, and the run still completes.
    wkb = await _wkb_polygon(
        store, "POLYGON((0.010 0.008,0.020 0.008,0.020 0.012,0.010 0.012,0.010 0.008))"
    )
    svc = RunService(
        store,
        settings,
        overpass=FakeOverpass(synthetic_elements()),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )
    run = await _persist(svc, store, settings, DEFAULT_WEIGHTS, clip_polygons=[wkb])
    assert run.status == "done"
    assert run.candidate_count > 0
    gj = await store.get_run_geojson(run.id)
    for feat in gj["features"]:
        lon, _lat = feat["geometry"]["coordinates"]
        assert lon >= 0.010 - 1e-9, f"candidate at lon {lon} leaked outside the clip"
    # The unclipped run over the same area scores strictly more points.
    full = await _run(store, settings, synthetic_elements(), DEFAULT_WEIGHTS)
    assert run.candidate_count < full.candidate_count


async def test_multiple_clips_intersect(store, settings):
    # Two clips AND together (e.g. a forest boundary AND public land): a candidate
    # must lie inside BOTH. Clip A = lon >= 0.010, clip B = lon >= 0.014, so only
    # lon >= 0.014 survives.
    a = await _wkb_polygon(
        store, "POLYGON((0.010 0.008,0.020 0.008,0.020 0.012,0.010 0.012,0.010 0.008))"
    )
    b = await _wkb_polygon(
        store, "POLYGON((0.014 0.008,0.020 0.008,0.020 0.012,0.014 0.012,0.014 0.008))"
    )
    svc = RunService(
        store, settings,
        overpass=FakeOverpass(synthetic_elements()),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )
    run = await _persist(svc, store, settings, DEFAULT_WEIGHTS, clip_polygons=[a, b])
    assert run.status == "done" and run.candidate_count > 0
    gj = await store.get_run_geojson(run.id)
    for feat in gj["features"]:
        lon, _lat = feat["geometry"]["coordinates"]
        assert lon >= 0.014 - 1e-9, f"candidate at lon {lon} escaped the clip intersection"


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


def _svc(store, settings):
    return RunService(
        store,
        settings,
        overpass=FakeOverpass(synthetic_elements()),
        terrain_factory=fake_terrain_factory(slope_deg=3.0),
    )


async def test_tlc4_wallclock_bound_is_honored(store, settings):
    # A zero deadline trips the first checkpoint -> the run fails typed
    # (WallClockExceeded), never silently (TLC-4/10). Proves _execute gates on
    # the bound it is handed, not a hardcoded constant.
    svc = _svc(store, settings)
    b = TEST_BBOX.normalized()
    params = await store.scoring_config_params(SCORING_CONFIG_VERSION)
    run_id = await store.create_run(b, DEFAULT_WEIGHTS, SCORING_CONFIG_VERSION)
    await svc._execute(run_id, b, DEFAULT_WEIGHTS, params, 0.0)
    run = await store.get_run(run_id)
    assert run.status == "failed"
    assert "WallClockExceeded" in run.failure_reason
