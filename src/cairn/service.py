"""Run orchestration (UoW-1): score a bbox end to end.

Wires clients -> ingest -> engine -> store with the cross-cutting guarantees:
input validation before any fetch (TLC-4), provenance recorded before done
(TLC-2), typed failure surfaced on the run row (TLC-10), and a wall-clock
bound for the sync path (TLC-4). This module does I/O and may read the clock;
the TLC-1 purity rule constrains only the engine's scoring/normalization.
"""

from __future__ import annotations

import logging
import time

from cairn.clients.overpass import OverpassClient
from cairn.clients.terrain import TerrainSampler, tile_covering
from cairn.config import Settings
from cairn.engine.candidates import find_termini, generate_candidates, grid_cell
from cairn.engine.features import extract_features
from cairn.engine.scoring import score, select_distinct
from cairn.engine.scoring_config import SCORING_CONFIG_VERSION
from cairn.engine.types import Candidate, CandidateFeatures
from cairn.errors import (
    BboxTooLarge,
    CairnError,
    InvalidBbox,
    InvalidWeights,
    MvumError,
    StructuresError,
    WallClockExceeded,
)
from cairn.ingest.mvum import ingest_mvum
from cairn.ingest.osm import ingest_osm
from cairn.ingest.usa_structures import ingest_structures
from cairn.store import Store
from cairn.store.types import Bbox

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
        mvum=None,
        structures=None,
    ) -> None:
        self._store = store
        self._s = settings
        self._overpass = overpass
        self._terrain_factory = terrain_factory
        self._mvum = mvum  # USFS forest roads; None => OSM-only
        self._structures = structures  # USA Structures buildings; None => OSM-only

    async def score_view(
        self, bbox: Bbox, weights: dict[str, float],
        clip_polygons: list[bytes] | None = None,
    ) -> dict:
        """Roving "Score this view" (FC-17, doc #34): score `bbox` synchronously
        and return the ranked-results GeoJSON DIRECTLY — no persisted run.

        The engine pipeline is run-keyed, so this uses a SCRATCH run that is
        deleted on completion (FK cascade drops its candidates/features/scores).
        Observable contract (TLC-15): the `runs` table is unchanged afterward and
        the result never appears in the Runs list. Enforces the sync-area cap
        (TLC-18) — over it raises `BboxTooLarge` before any work. The viewport is
        always ≤ cap in normal use (the UI scores the cap rectangle)."""
        b = validate_request(bbox, weights, self._s)
        if b.area_km2() > self._s.sync_area_threshold_km2:
            raise BboxTooLarge(b.area_km2(), self._s.sync_area_threshold_km2)
        params = await self._store.scoring_config_params(SCORING_CONFIG_VERSION)
        run_id = await self._store.create_run(b, weights, SCORING_CONFIG_VERSION)
        try:
            await self._execute(
                run_id, b, weights, params, self._s.max_wallclock_seconds, clip_polygons
            )
            return await self._store.get_run_geojson(run_id)
        finally:
            await self._store.delete_run(run_id)  # scratch — nothing persists

    async def _execute(
        self, run_id: int, b: Bbox, weights: dict[str, float], params: dict,
        wallclock_s: float, clip_polygons: list[bytes] | None = None,
    ) -> None:
        """Run steps 2-8 of UoW-1, persisting status transitions. Never raises
        out (TLC-10): any failure is recorded on the run row. `wallclock_s` is
        the deadline for this run — tight on the sync path, looser for async
        background jobs (TLC-4)."""
        started = time.monotonic()

        def check_clock(stage: str) -> None:
            if time.monotonic() - started > wallclock_s:
                raise WallClockExceeded(wallclock_s)

        try:
            await self._store.set_run_status(run_id, "running")

            # 2-4: ensure OSM extract + chunked ingest.
            osm = await self._overpass.fetch(b)
            counts = await ingest_osm(
                self._store, osm.elements, chunk_size=self._s.ingest_chunk_size
            )

            # 4b: USFS MVUM forest roads (fills OSM's forest-road gaps). Best
            # effort — degrade to OSM-only if MVUM is unavailable (TLC-10).
            mvum_provenance: dict | None = None
            if self._mvum is not None:
                try:
                    mvum = await self._mvum.fetch(b)
                    mvum_count = await ingest_mvum(
                        self._store, mvum.features, chunk_size=self._s.ingest_chunk_size
                    )
                    mvum_provenance = {
                        "source": "usfs_mvum",
                        "cache_key": mvum.cache_key,
                        "fetched_at": mvum.fetched_at,
                        "from_cache": mvum.from_cache,
                        "road_count": mvum_count,
                    }
                except MvumError as exc:
                    log.warning("MVUM unavailable, OSM-only roads: %s", exc)
                    mvum_provenance = {"source": "usfs_mvum", "error": str(exc)}

            # 4c: USA Structures buildings (fills OSM's rural building gaps so
            # the no-camping-near-houses setback works). Best effort (TLC-10).
            struct_provenance: dict | None = None
            if self._structures is not None:
                try:
                    st = await self._structures.fetch(b)
                    st_count = await ingest_structures(
                        self._store, st.features, chunk_size=self._s.ingest_chunk_size
                    )
                    struct_provenance = {
                        "source": "usa_structures",
                        "cache_key": st.cache_key,
                        "fetched_at": st.fetched_at,
                        "from_cache": st.from_cache,
                        "building_count": st_count,
                    }
                except StructuresError as exc:
                    log.warning("USA Structures unavailable, OSM-only buildings: %s", exc)
                    struct_provenance = {"source": "usa_structures", "error": str(exc)}
            check_clock("ingest")

            # 5: candidates (uniform sampling + injected dead-end termini).
            ways = await self._store.load_drivable_ways(b)
            candidates, capped = generate_candidates(
                ways,
                spacing_m=self._s.candidate_spacing_m,
                grid_m=self._s.candidate_grid_m,
                max_candidates=self._s.max_candidates,
            )
            ways_full = await self._store.load_drivable_ways(b, clip=False)
            tol = params["features"]["road_end"].get("terminus_tol_m", 20.0)
            termini = find_termini(ways_full, b, tol_m=tol)
            candidates = _inject_termini(
                candidates, termini, self._s.candidate_grid_m, self._s.max_candidates
            )
            # Search-region clips (score-within-boundary forest/park polygon
            # and/or public-land-only): keep candidates inside ALL clips. A
            # search-region filter like the bbox, applied here in the service over
            # candidate points — the engine never sees it (TLC-3). Each uses the
            # EXACT polygon so edge spots aren't dropped.
            if clip_polygons:
                before = len(candidates)
                for poly in clip_polygons:
                    inside = await self._store.points_within_polygon(
                        [(c.lon, c.lat) for c in candidates], poly
                    )
                    candidates = [c for c, keep in zip(candidates, inside) if keep]
                log.info(
                    "run %d: %d clip(s) %d -> %d candidates",
                    run_id, len(clip_polygons), before, len(candidates),
                )
            id_candidates = await self._store.insert_candidates(run_id, candidates, capped)
            log.info(
                "run %d: %d ways -> %d candidates (%d termini, capped=%s)",
                run_id,
                len(ways),
                len(candidates),
                len(termini),
                capped,
            )

            # 6: features.
            present = await self._store.classes_present(b)
            road_tags = await self._store.drivable_way_tags(b)
            # Candidates whose point sits alongside a USFS MVUM forest road get
            # forest road_character even if sampled from a mistagged OSM copy.
            mvum_near = await self._store.candidates_near_mvum(run_id, self._s.mvum_overlap_m)
            terrain = self._terrain_factory(self._s, zoom=self._s.terrarium_zoom)
            feature_rows: list[tuple] = []
            cfs: list[CandidateFeatures] = []
            try:
                for cid, cand in id_candidates:
                    cf = await extract_features(
                        self._store, terrain, cand, params, present, road_tags, termini,
                        self._s.building_setback_m, forest_overlap=cid in mvum_near,
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
                "mvum": mvum_provenance,
                "structures": struct_provenance,
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


def _inject_termini(
    candidates: list[Candidate],
    termini: list[tuple[float, float, int]],
    grid_m: float,
    max_candidates: int,
) -> list[Candidate]:
    """Add dead-end terminus points as candidates so every spur-end is a scored,
    recommendable site. A terminus is the meaningful spot at the end of a spur,
    so when it lands in the same dedup grid cell as a uniform sample it REPLACES
    that sample (the candidate snaps onto the dead-end) rather than being
    discarded. New cells are capped at max_candidates; a replacement never grows
    the list. First terminus wins a cell (no terminus replaces another)."""
    out = list(candidates)
    by_cell = {grid_cell(c.lon, c.lat, grid_m): i for i, c in enumerate(out)}
    terminus_cells: set[tuple] = set()
    for lon, lat, way_id in termini:
        cell = grid_cell(lon, lat, grid_m)
        if cell in terminus_cells:
            continue  # another dead-end already owns this cell
        cand = Candidate(lon=lon, lat=lat, source_way_id=way_id)
        if cell in by_cell:
            out[by_cell[cell]] = cand  # snap the uniform sample onto the dead-end
        elif len(out) < max_candidates:
            by_cell[cell] = len(out)
            out.append(cand)
        else:
            continue
        terminus_cells.add(cell)
    return out


def _feature_row(cid: int, cf: CandidateFeatures) -> tuple:
    f = cf.features
    fl, wa, fo, so, rd, rm, re = (
        f["flatness"],
        f["water_proximity"],
        f["tree_cover"],
        f["solitude"],
        f["road_character"],
        f["remoteness"],
        f["road_end"],
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
        rm.value,
        1 if cf.rough_road else 0,
        re.value,
        0 if re.available else 1,
    )


def _now_iso() -> str:
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()
    )
