"""Run orchestration (UoW-1): score a bbox end to end.

Wires clients -> ingest -> engine -> store with the cross-cutting guarantees:
input validation before any fetch (TLC-4), provenance recorded before done
(TLC-2), typed failure surfaced on the run row (TLC-10), and a wall-clock
bound for the sync path (TLC-4). This module does I/O and may read the clock;
the TLC-1 purity rule constrains only the engine's scoring/normalization.
"""

from __future__ import annotations

import asyncio
import logging
import time

from cairn.clients.overpass import OverpassClient
from cairn.clients.terrain import TerrainSampler, tile_covering
from cairn.config import Settings
from cairn.engine.candidates import generate_candidates
from cairn.engine.features import extract_features
from cairn.engine.scoring import score, select_distinct
from cairn.engine.scoring_config import SCORING_CONFIG_VERSION
from cairn.engine.types import CandidateFeatures
from cairn.errors import (
    BboxTooLarge,
    CairnError,
    InvalidBbox,
    InvalidWeights,
    WallClockExceeded,
)
from cairn.ingest.osm import ingest_osm
from cairn.store import Store
from cairn.store.types import Bbox, Run

log = logging.getLogger("cairn.service")


def validate_request(bbox: Bbox, weights: dict[str, float], settings: Settings) -> Bbox:
    """Validate + normalize a score request BEFORE any fetch/DB write (TLC-4)."""
    b = bbox.normalized()
    if not (-180 <= b.min_lon < b.max_lon <= 180 and -90 <= b.min_lat < b.max_lat <= 90):
        raise InvalidBbox(f"degenerate or out-of-range bbox: {b}")
    area = b.area_km2()
    if area > settings.max_bbox_area_km2:
        raise BboxTooLarge(area, settings.max_bbox_area_km2)
    if any(w < 0 for w in weights.values()):
        raise InvalidWeights("weights must be non-negative")
    if not any(w > 0 for w in weights.values()):
        raise InvalidWeights("at least one weight must be positive")
    return b


class RunService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        overpass: OverpassClient,
        terrain_factory=TerrainSampler,
    ) -> None:
        self._store = store
        self._s = settings
        self._overpass = overpass
        self._terrain_factory = terrain_factory

    async def score_bbox(self, bbox: Bbox, weights: dict[str, float]) -> Run:
        """Execute UoW-1 synchronously. Returns the finished (done|failed) Run.

        Validation (TLC-4) raises BEFORE any run row or fetch."""
        b = validate_request(bbox, weights, self._s)
        params = await self._store.scoring_config_params(SCORING_CONFIG_VERSION)
        run_id = await self._store.create_run(b, weights, SCORING_CONFIG_VERSION)
        await self._execute(run_id, b, weights, params)
        return await self._store.get_run(run_id)

    async def start_async(self, bbox: Bbox, weights: dict[str, float]) -> int:
        """Validate, create the run row, and schedule execution in the
        background (TLC-4 async path). Returns the run_id immediately; the
        client polls GET /runs/{id}. Validation still raises synchronously."""
        b = validate_request(bbox, weights, self._s)
        params = await self._store.scoring_config_params(SCORING_CONFIG_VERSION)
        run_id = await self._store.create_run(b, weights, SCORING_CONFIG_VERSION)
        asyncio.create_task(self._execute(run_id, b, weights, params))
        return run_id

    async def _execute(
        self, run_id: int, b: Bbox, weights: dict[str, float], params: dict
    ) -> None:
        """Run steps 2-8 of UoW-1, persisting status transitions. Never raises
        out (TLC-10): any failure is recorded on the run row."""
        started = time.monotonic()

        def check_clock(stage: str) -> None:
            if time.monotonic() - started > self._s.max_wallclock_seconds:
                raise WallClockExceeded(self._s.max_wallclock_seconds)

        try:
            await self._store.set_run_status(run_id, "running")

            # 2-4: ensure OSM extract + chunked ingest.
            osm = await self._overpass.fetch(b)
            counts = await ingest_osm(
                self._store, osm.elements, chunk_size=self._s.ingest_chunk_size
            )
            check_clock("ingest")

            # 5: candidates.
            ways = await self._store.load_drivable_ways(b)
            candidates, capped = generate_candidates(
                ways,
                spacing_m=self._s.candidate_spacing_m,
                grid_m=self._s.candidate_grid_m,
                max_candidates=self._s.max_candidates,
            )
            id_candidates = await self._store.insert_candidates(run_id, candidates, capped)
            log.info(
                "run %d: %d ways -> %d candidates (capped=%s)",
                run_id,
                len(ways),
                len(candidates),
                capped,
            )

            # 6: features.
            present = await self._store.classes_present(b)
            road_tags = await self._store.drivable_way_tags(b)
            terrain = self._terrain_factory(self._s, zoom=self._s.terrarium_zoom)
            feature_rows: list[tuple] = []
            cfs: list[CandidateFeatures] = []
            try:
                for cid, cand in id_candidates:
                    cf = await extract_features(
                        self._store, terrain, cand, params, present, road_tags
                    )
                    cfs.append(cf)
                    feature_rows.append(_feature_row(cid, cf))
                    check_clock("features")
            finally:
                await terrain.aclose()
            await self._store.insert_candidate_features(feature_rows)

            # 7: score (pure) + distinct-site selection (pure NMS) + persist.
            scored = score(cfs, weights, params)
            scored = select_distinct(scored, self._s.distinct_radius_m)
            distinct_count = sum(1 for sc in scored if sc.distinct)
            id_map = {
                (c.lon, c.lat, c.source_way_id): cid for cid, c in id_candidates
            }
            await self._store.insert_candidate_scores(scored, id_map)

            # 8: provenance + done (TLC-2).
            run_degraded = sorted({d for sc in scored for d in sc.degraded_features})
            provenance = {
                "generated_at": _now_iso(),
                "scoring_config_version": SCORING_CONFIG_VERSION,
                "weights": weights,
                "distinct_radius_m": self._s.distinct_radius_m,
                "bbox": [b.min_lon, b.min_lat, b.max_lon, b.max_lat],
                "osm": {
                    "source": "overpass",
                    "query_version": osm.query_version,
                    "cache_key": osm.cache_key,
                    "fetched_at": osm.fetched_at,
                    "from_cache": osm.from_cache,
                    "class_counts": counts,
                },
                "elevation": {
                    "source": "terrarium",
                    "covering": tile_covering(b, self._s.terrarium_zoom),
                    "tiles_sampled": len(terrain.tiles_used),
                },
            }
            await self._store.finish_run(
                run_id,
                provenance=provenance,
                candidate_count=len(candidates),
                distinct_count=distinct_count,
                capped=capped,
                degraded_features=run_degraded,
            )
        except CairnError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("run %d failed: %s", run_id, reason)
            await self._store.fail_run(run_id, reason)
        except Exception as exc:  # unexpected — still surface, never swallow
            log.exception("run %d crashed", run_id)
            await self._store.fail_run(run_id, f"InternalError: {exc}")


def _feature_row(cid: int, cf: CandidateFeatures) -> tuple:
    f = cf.features
    fl, wa, fo, so, rd = (
        f["flatness"],
        f["water_proximity"],
        f["tree_cover"],
        f["solitude"],
        f["road_character"],
    )
    return (
        cid,
        fl.value,
        0 if fl.available else 1,
        wa.value,
        0 if wa.available else 1,
        fo.value,
        0 if fo.available else 1,
        so.value,
        0 if so.available else 1,
        1 if cf.near_campsite else 0,
        rd.value,
        0 if rd.available else 1,
    )


def _now_iso() -> str:
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()
    )
