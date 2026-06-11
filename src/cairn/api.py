"""Web API (FC-8).

FastAPI. Validates bbox + weights BEFORE any fetch/DB write (TLC-4), delegates
all scoring/geo work to the engine via RunService (no scoring logic here), maps
typed engine errors to HTTP codes, and returns `failed` runs as status+reason
(not a 500, TLC-10). Every results response carries the TLC-3 disclaimer.

Endpoints: POST /score, GET /runs, GET /runs/{id}, GET /runs/{id}/geojson,
GET /runs/{id}/roads, GET /overlays, GET /overlays/{layer},
GET /nearby-campgrounds, GET /saved, POST /saved, PATCH/DELETE /saved/{id},
GET /suggest-name, GET /geocode, GET /config, GET /health, and the static UI
at / (mounted if web/ exists).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cairn import DISCLAIMER, __version__
from cairn.clients.mvum import MvumClient
from cairn.clients.nominatim import GeocodeClient
from cairn.clients.overpass import OverpassClient
from cairn.clients.usa_structures import StructuresClient
from cairn.config import get_settings
from cairn.errors import GeocodeError, RunNotFound, ValidationError
from cairn.logging import configure
from cairn.service import RunService
from cairn.store import Store
from cairn.store.overlays import DISCLAIMER as OVERLAY_DISCLAIMER
from cairn.store.overlays import OverlayStore
from cairn.store.repo import UNSET
from cairn.store.types import Bbox

log = logging.getLogger("cairn.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure(settings.log_level)
    log.info("opening store at %s", settings.db_path)
    store = await Store.open(settings)
    overlays = await OverlayStore.open(settings)
    overpass = OverpassClient(settings)
    mvum = MvumClient(settings) if settings.mvum_enabled else None
    structures = StructuresClient(settings) if settings.structures_enabled else None
    geocoder = GeocodeClient(settings) if settings.geocode_enabled else None
    app.state.settings = settings
    app.state.store = store
    app.state.overlays = overlays
    app.state.overpass = overpass
    app.state.mvum = mvum
    app.state.structures = structures
    app.state.geocoder = geocoder
    app.state.service = RunService(
        store, settings, overpass=overpass, mvum=mvum, structures=structures
    )
    log.info(
        "cairn %s ready on %s:%s (mvum=%s structures=%s overlays=%s)",
        __version__,
        settings.host,
        settings.port,
        "on" if mvum else "off",
        "on" if structures else "off",
        "on" if overlays.available else "off",
    )
    try:
        yield
    finally:
        await overpass.aclose()
        if mvum is not None:
            await mvum.aclose()
        if structures is not None:
            await structures.aclose()
        if geocoder is not None:
            await geocoder.aclose()
        await overlays.close()
        await store.close()


app = FastAPI(title="Cairn", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    """Make the browser revalidate the UI assets every load (cheap 304 via the
    StaticFiles ETag when unchanged) so a rebuild shows up without a hard refresh
    — instead of serving a stale cached app.js/app.css off heuristic caching."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- request models --------------------------------------------------------


class ScoreRequest(BaseModel):
    bbox: list[float] = Field(
        ..., min_length=4, max_length=4, description="[min_lon, min_lat, max_lon, max_lat]"
    )
    weights: dict[str, float] | None = None
    run_async: bool | None = Field(
        default=None,
        alias="async",
        description="Force background execution; default decides by bbox area.",
    )

    model_config = {"populate_by_name": True}


class SavedRequest(BaseModel):
    kind: str = Field(..., description="'site' or 'campground'")
    name: str | None = None
    lat: float
    lon: float
    payload: dict | None = None
    note: str | None = None
    rating: float | None = Field(default=None, description="quality rating, 0-5 stars")


class SavedUpdateRequest(BaseModel):
    # Partial update: a field left unset is not written (model_fields_set tracks
    # which were sent), so editing one of name/rating/note keeps the rest.
    name: str | None = None
    rating: float | None = Field(default=None, description="quality rating, 0-5 stars")
    note: str | None = None


# --- error handling --------------------------------------------------------


@app.exception_handler(ValidationError)
async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    # Bbox/weights validation, incl. BboxTooLarge — name the bound (TLC-4).
    return JSONResponse(status_code=422, content={"error": type(exc).__name__, "detail": str(exc)})


@app.exception_handler(RunNotFound)
async def _notfound_handler(request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "RunNotFound", "detail": str(exc)})


# --- helpers ---------------------------------------------------------------


def _run_summary(run) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "bbox": [run.bbox.min_lon, run.bbox.min_lat, run.bbox.max_lon, run.bbox.max_lat],
        "weights": run.weights,
        "scoring_config_version": run.scoring_config_version,
        "candidate_count": run.candidate_count,
        "distinct_count": run.distinct_count,
        "capped": run.capped,
        "degraded_features": run.degraded_features,
        "failure_reason": run.failure_reason,
        "provenance": run.provenance,
        "created_at": run.created_at,
        "done_at": run.done_at,
        "disclaimer": DISCLAIMER,
    }


# --- endpoints -------------------------------------------------------------


@app.post("/score")
async def score(req: ScoreRequest, request: Request) -> JSONResponse:
    settings = request.app.state.settings
    service: RunService = request.app.state.service
    bbox = Bbox(*req.bbox)
    weights = req.weights or settings.ui_config()["default_weights"]

    # Decide sync vs async (TLC-4). Validation happens inside the service and
    # raises ValidationError -> 422 before any run row is created.
    go_async = req.run_async
    if go_async is None:
        go_async = bbox.normalized().area_km2() > settings.sync_area_threshold_km2

    if go_async:
        run_id = await service.start_async(bbox, weights)
        run = await request.app.state.store.get_run(run_id)
        return JSONResponse(status_code=202, content=_run_summary(run))

    run = await service.score_bbox(bbox, weights)
    return JSONResponse(status_code=200, content=_run_summary(run))


@app.get("/runs")
async def list_runs(request: Request, limit: int = 50) -> dict:
    store: Store = request.app.state.store
    runs = await store.list_runs(limit=limit)
    return {"runs": [_run_summary(r) for r in runs], "disclaimer": DISCLAIMER}


@app.get("/runs/{run_id}")
async def get_run(run_id: int, request: Request) -> dict:
    store: Store = request.app.state.store
    run = await store.get_run(run_id)  # raises RunNotFound -> 404
    return _run_summary(run)


@app.get("/runs/{run_id}/geojson")
async def get_run_geojson(run_id: int, request: Request, top_n: int | None = None) -> dict:
    store: Store = request.app.state.store
    await store.get_run(run_id)  # 404 if missing
    return await store.get_run_geojson(run_id, top_n=top_n)


@app.get("/runs/{run_id}/roads")
async def get_run_roads(run_id: int, request: Request) -> dict:
    store: Store = request.app.state.store
    await store.get_run(run_id)  # 404 if missing
    return await store.get_run_roads_geojson(run_id)


@app.get("/overlays")
async def list_overlays(request: Request) -> dict:
    """Available display-only land layers (TLC-3) — empty if overlays.db absent."""
    overlays: OverlayStore = request.app.state.overlays
    return {"layers": await overlays.layers(), "disclaimer": OVERLAY_DISCLAIMER}


@app.get("/overlays/{layer}")
async def get_overlay(layer: str, request: Request, bbox: str) -> dict:
    """GeoJSON for one land layer within `bbox` (clipped, capped). Display-only."""
    overlays: OverlayStore = request.app.state.overlays
    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError
    except ValueError:
        raise ValidationError("bbox must be 'min_lon,min_lat,max_lon,max_lat'") from None
    return await overlays.query(layer, Bbox(*parts))


@app.get("/saved")
async def list_saved(request: Request) -> dict:
    """The user's saved places (favorites — dispersed sites + campgrounds)."""
    store: Store = request.app.state.store
    return {"saved": await store.list_saved()}


def _check_rating(rating: float | None) -> None:
    if rating is not None and not (0 <= rating <= 5):
        raise ValidationError("rating must be between 0 and 5")


@app.post("/saved")
async def add_saved(req: SavedRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    if req.kind not in ("site", "campground"):
        raise ValidationError("kind must be 'site' or 'campground'")
    _check_rating(req.rating)
    row = await store.add_saved(
        req.kind, req.name, req.lat, req.lon, req.payload, req.note, req.rating
    )
    return JSONResponse(status_code=201, content=row)


@app.delete("/saved/{saved_id}")
async def delete_saved(saved_id: int, request: Request) -> dict:
    store: Store = request.app.state.store
    return {"deleted": await store.delete_saved(saved_id)}


@app.patch("/saved/{saved_id}")
async def update_saved(saved_id: int, req: SavedUpdateRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    _check_rating(req.rating)
    # Only write fields the client actually sent (partial update).
    sent = req.model_fields_set
    row = await store.update_saved(
        saved_id,
        name=req.name if "name" in sent else UNSET,
        rating=req.rating if "rating" in sent else UNSET,
        note=req.note if "note" in sent else UNSET,
    )
    if row is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return JSONResponse(status_code=200, content=row)


@app.get("/suggest-name")
async def suggest_name(request: Request, lat: float, lon: float) -> dict:
    """A friendly default name for a scored site — nearest named lake / source
    road from our own OSM extract, else a Nominatim reverse-geocode fallback."""
    store: Store = request.app.state.store
    name = await store.suggest_site_name(lat, lon)
    if not name:
        geocoder: GeocodeClient | None = request.app.state.geocoder
        if geocoder is not None:
            try:
                name = await geocoder.reverse(lat, lon)
            except GeocodeError:
                name = None
    return {"name": name}


@app.get("/nearby-campgrounds")
async def nearby_campgrounds(
    request: Request, lat: float, lon: float, limit: int = 5
) -> dict:
    """The nearest established campgrounds to a point (display-only — TLC-3).
    Annotates a scored dispersed site with 'book a spot' fallbacks; never feeds
    scoring."""
    overlays: OverlayStore = request.app.state.overlays
    return {"campgrounds": await overlays.nearest_campgrounds(lat, lon, limit=limit)}


@app.get("/geocode")
async def geocode(request: Request, q: str, viewbox: str | None = None) -> dict:
    """Forward-geocode a place name via Nominatim (keyless). Returns
    {results: [{display_name, lat, lon, bbox?}]}. 404 if geocoding is disabled;
    502 if Nominatim is unreachable/errored."""
    geocoder: GeocodeClient | None = request.app.state.geocoder
    if geocoder is None:
        return JSONResponse(status_code=404, content={"error": "GeocodeDisabled"})
    if not q or not q.strip():
        raise ValidationError("q (place name) is required")
    try:
        results = await geocoder.search(q, viewbox=viewbox)
    except GeocodeError as exc:
        return JSONResponse(
            status_code=502, content={"error": type(exc).__name__, "detail": str(exc)}
        )
    return {"results": results}


@app.get("/config")
async def config(request: Request) -> dict:
    settings = request.app.state.settings
    return {**settings.ui_config(), "disclaimer": DISCLAIMER}


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    try:
        info = await store.health()
    except Exception as exc:
        log.exception("health check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "version": __version__, "error": str(exc)},
        )
    healthy = bool(info.get("caches_writable")) and info.get("schema_version") is not None
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "disclaimer": DISCLAIMER,
            **info,
        },
    )


# --- static UI (FC-9) — mounted last so it doesn't shadow the API routes ---
_WEB_DIR = Path(__file__).parent / "web"
if (_WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
