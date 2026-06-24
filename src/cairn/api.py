"""Web API (FC-8).

FastAPI. Validates bbox + weights BEFORE any fetch/DB write (TLC-4), delegates
all scoring/geo work to the engine via RunService (no scoring logic here), maps
typed engine errors to HTTP codes, and returns `failed` runs as status+reason
(not a 500, TLC-10). Every results response carries the TLC-3 disclaimer.

Endpoints: POST /score, GET /runs, GET/PATCH/DELETE /runs/{id}, GET /runs/{id}/geojson,
GET /runs/{id}/roads, GET /roads, GET /overlays, GET /overlays/{layer},
GET /overlays/{layer}/shape,
GET /nearby-campgrounds, GET /saved, POST /saved, PATCH/DELETE /saved/{id},
PUT /saved/{id}/collections, GET/POST /collections,
PATCH/DELETE /collections/{id}, POST /collections/{id}/places,
GET/POST /trips, GET/PATCH/DELETE /trips/{id}, POST /trips/{id}/members,
DELETE /trips/{id}/members/{saved_id}, GET /trips/{id}/export.gpx|.kml,
GET /trips/{id}/availability,
GET /suggest-name, GET /geocode, GET /config, GET /health, and the static UI
at / (mounted if web/ exists).
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cairn import DISCLAIMER, __version__
from cairn.availability import trip_availability
from cairn.clients.availability import RecreationGovClient
from cairn.clients.mvum import MvumClient
from cairn.clients.nominatim import GeocodeClient
from cairn.clients.overpass import OverpassClient
from cairn.clients.usa_structures import StructuresClient
from cairn.clients.usedirect import UseDirectClient
from cairn.config import get_settings
from cairn.errors import GeocodeError, RunNotFound, ValidationError
from cairn.export import trip_slug, trip_to_gpx, trip_to_kml
from cairn.logging import configure
from cairn.service import RunService
from cairn.store import Store
from cairn.store.overlays import DISCLAIMER as OVERLAY_DISCLAIMER
from cairn.store.overlays import OverlayStore
from cairn.store.repo import UNSET
from cairn.store.types import Bbox

log = logging.getLogger("cairn.api")


def _load_usedirect_map() -> dict:
    """The committed MN marker-name -> UseDirect FacilityId table (display-only).
    Degradable: a missing/unreadable file just means MN markers link out."""
    candidates = [
        files("cairn") / "data" / "usedirect_mn.json",  # packaged (wheel)
        Path(__file__).parent / "data" / "usedirect_mn.json",  # source tree
    ]
    for path in candidates:
        try:
            return json.loads(Path(str(path)).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal to boot
            log.warning("usedirect mapping unreadable (%s); MN availability off", exc)
            return {}
    log.warning("usedirect mapping file not found; MN availability off")
    return {}


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
    availability = (
        RecreationGovClient(settings) if settings.availability_enabled else None
    )
    usedirect = UseDirectClient(settings) if settings.usedirect_enabled else None
    usedirect_map = _load_usedirect_map() if settings.usedirect_enabled else {}
    app.state.settings = settings
    app.state.store = store
    app.state.overlays = overlays
    app.state.overpass = overpass
    app.state.mvum = mvum
    app.state.structures = structures
    app.state.geocoder = geocoder
    app.state.availability = availability
    app.state.usedirect = usedirect
    app.state.usedirect_map = usedirect_map
    app.state.service = RunService(
        store, settings, overpass=overpass, mvum=mvum, structures=structures
    )
    log.info(
        "cairn %s ready on %s:%s "
        "(mvum=%s structures=%s overlays=%s availability=%s usedirect=%s/%d)",
        __version__,
        settings.host,
        settings.port,
        "on" if mvum else "off",
        "on" if structures else "off",
        "on" if overlays.available else "off",
        "on" if availability else "off",
        "on" if usedirect else "off",
        len(usedirect_map),
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
        if availability is not None:
            await availability.aclose()
        if usedirect is not None:
            await usedirect.aclose()
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
    if path == "/" or path.endswith(".html"):
        # The entry HTML must never be cached: iOS Safari was serving a stale
        # index.html (ignoring no-cache revalidation), so every UI update was
        # invisible on-device. no-store forces a fresh fetch every load; the
        # HTML then points at content-hashed app.js/app.css (?v=…) for caching.
        response.headers["Cache-Control"] = "no-store"
    elif path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- request models --------------------------------------------------------


class ClipSpec(BaseModel):
    """Score-within-boundary: clip candidates to this forest/park polygon."""

    layer: str
    name: str


class ScoreRequest(BaseModel):
    bbox: list[float] = Field(
        ..., min_length=4, max_length=4, description="[min_lon, min_lat, max_lon, max_lat]"
    )
    weights: dict[str, float] | None = None
    clip: ClipSpec | None = Field(
        default=None,
        description="Optional boundary clip — score only candidates inside this land shape.",
    )
    public_only: bool = Field(
        default=False,
        description="Restrict candidates to public-ownership land (drops private inholdings).",
    )


class SavedRequest(BaseModel):
    kind: str = Field(..., description="'site', 'campground', or 'dispersed'")
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


class TripRequest(BaseModel):
    name: str
    start_date: str | None = Field(default=None, description="ISO date 'YYYY-MM-DD'")
    end_date: str | None = Field(default=None, description="ISO date 'YYYY-MM-DD'")
    note: str | None = None


class TripUpdateRequest(BaseModel):
    # Partial update (model_fields_set tracks which fields were sent).
    name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    note: str | None = None


class MemberRequest(BaseModel):
    saved_id: int
    role: str | None = Field(default="primary", description="'primary' or 'fallback'")


class CollectionRequest(BaseModel):
    name: str
    color: str | None = Field(default=None, description="hex swatch, e.g. '#5b9c5a'")


class CollectionUpdateRequest(BaseModel):
    # Partial update (model_fields_set tracks which fields were sent).
    name: str | None = None
    color: str | None = None
    position: int | None = None


class PlaceCollectionsRequest(BaseModel):
    collection_ids: list[int] = Field(default_factory=list)


class BulkPlacesRequest(BaseModel):
    saved_ids: list[int] = Field(default_factory=list)


class ForestMarksRequest(BaseModel):
    pinned: list[str] = Field(default_factory=list)
    recent: list[str] = Field(default_factory=list)


# --- error handling --------------------------------------------------------


@app.exception_handler(ValidationError)
async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    # Bbox/weights validation, incl. BboxTooLarge — name the bound (TLC-4).
    return JSONResponse(status_code=422, content={"error": type(exc).__name__, "detail": str(exc)})


@app.exception_handler(RunNotFound)
async def _notfound_handler(request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "RunNotFound", "detail": str(exc)})


# --- helpers ---------------------------------------------------------------


# --- endpoints -------------------------------------------------------------


async def _resolve_clips(req: ScoreRequest, bbox: Bbox, overlays: OverlayStore):
    """Search-region clips (score-within-boundary forest/park + public-land-only),
    resolved to plain WKB so RunService never depends on OverlayStore (TLC-3).
    Multiple clips AND together in the service. Returns a list or None."""
    clip_polygons: list[bytes] = []
    if req.clip is not None:
        g = await overlays.get_clip_geometry(req.clip.layer, req.clip.name, bbox)
        if g is None:
            raise ValidationError(f"unknown clip shape: {req.clip.layer}/{req.clip.name}")
        clip_polygons.append(g)
    if req.public_only:
        g = await overlays.get_public_land_geometry(bbox)
        if g is None:
            # No ownership parcels here — opt-in coverage means we can't tell
            # private from no-data, so refuse rather than silently filter all out.
            raise ValidationError(
                "no public-land ownership data for this area — can't filter to public land"
            )
        clip_polygons.append(g)
    return clip_polygons or None


@app.post("/score/view")
async def score_view(req: ScoreRequest, request: Request) -> dict:
    """Roving "Score this view" (FC-17): score the bbox synchronously and return
    the ranked-results GeoJSON directly — no persisted run (TLC-15). Over the
    sync-area cap → 422 BboxTooLarge (TLC-18). `clip`/`public_only` restrict
    candidates exactly as POST /score does."""
    settings = request.app.state.settings
    service: RunService = request.app.state.service
    bbox = Bbox(*req.bbox)
    weights = req.weights or settings.ui_config()["default_weights"]
    clips = await _resolve_clips(req, bbox, request.app.state.overlays)
    return await service.score_view(bbox, weights, clip_polygons=clips)


@app.get("/roads")
async def get_roads(request: Request, bbox: str) -> dict:
    """Drivable ways (OSM ∪ MVUM) within `bbox` — the viewport roads overlay,
    independent of any run, so forest spurs show wherever you browse (anywhere a
    prior run ingested road data). Clipped + capped. Display-only (TLC-3)."""
    store: Store = request.app.state.store
    settings = request.app.state.settings
    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError
    except ValueError:
        raise ValidationError("bbox must be 'min_lon,min_lat,max_lon,max_lat'") from None
    return await store.get_roads_geojson(Bbox(*parts), max_features=settings.overlay_max_features)


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


@app.get("/overlays/{layer}/shape")
async def get_overlay_shape(layer: str, request: Request, name: str):
    """The FULL outline of one named forest/park (all disjoint parts unioned) —
    a server-simplified geometry for the highlight plus the precise full-geometry
    bbox for zoom-to-extent. Unlike GET /overlays/{layer} this is NOT bbox-clipped
    (the browser only ever has the viewport slice). Display-only (TLC-3). 404 if
    no shape by that name exists."""
    overlays: OverlayStore = request.app.state.overlays
    shape = await overlays.get_shape(layer, name)
    if shape is None:
        return JSONResponse(
            status_code=404,
            content={"error": "ShapeNotFound", "detail": f"no {layer} shape named {name!r}"},
        )
    return shape


def _mvum_bounds(settings) -> list[list[float]]:
    """Bounds [s, w, n, e] of every loaded MVUM raster, or [] if none."""
    manifest = Path(settings.data_dir) / "mvum" / "manifest.json"
    if not manifest.exists():
        return []
    return [it["bounds"] for it in json.loads(manifest.read_text()) if it.get("bounds")]


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    """a = forest bbox [w, s, e, n]; b = MVUM bounds [s, w, n, e]."""
    aw, as_, ae, an = a
    bs, bw, bn, be = b
    return not (ae < bw or aw > be or an < bs or as_ > bn)


@app.get("/forests")
async def list_forests(request: Request) -> dict:
    """Browsable forests (national + state) with area + bbox + has_mvum — the
    Explore-tab list (doc #34). Display-only context (TLC-20). Empty if no
    overlays.db."""
    overlays: OverlayStore = request.app.state.overlays
    forests = await overlays.list_forests()
    mb = _mvum_bounds(request.app.state.settings)
    for f in forests:
        f["has_mvum"] = any(_bbox_intersects(f["bbox"], m) for m in mb)
    return {"forests": forests}


@app.get("/forests/summary")
async def forest_summary(request: Request, layer: str, name: str):
    """On-tap counts for one forest (dispersed + campgrounds inside, has_mvum).
    Display-only (TLC-20). 404 if the name doesn't exist."""
    overlays: OverlayStore = request.app.state.overlays
    summary = await overlays.forest_summary(layer, name)
    if summary is None:
        return JSONResponse(
            status_code=404,
            content={"error": "ForestNotFound", "detail": f"no {layer} named {name!r}"},
        )
    mb = _mvum_bounds(request.app.state.settings)
    summary["has_mvum"] = any(_bbox_intersects(summary["bbox"], m) for m in mb)
    return summary


@app.get("/forests/marks")
async def get_forest_marks(request: Request) -> dict:
    """The Explore tab's pinned + recent forests (cross-device; doc #34 #3).
    UI prefs only, never scoring (TLC-3)."""
    store: Store = request.app.state.store
    return await store.get_forest_marks()


@app.put("/forests/marks")
async def set_forest_marks(req: ForestMarksRequest, request: Request) -> dict:
    """Replace the pinned + recent forest lists wholesale (the client owns the
    ordering/trimming; this just persists it)."""
    store: Store = request.app.state.store
    return await store.set_forest_marks(req.pinned, req.recent)


@app.get("/mvum")
async def mvum_overlays(request: Request) -> dict:
    """Available MVUM raster overlays — georeferenced district map sheets draped
    as a display-only image layer (TLC-3). These are the rendered USFS MVUMs (the
    dispersed-camping corridors exist only as drawn symbols, not data). Empty if
    none are loaded (scripts/load_mvum.py)."""
    mdir = Path(request.app.state.settings.data_dir) / "mvum"
    manifest = mdir / "manifest.json"
    if not manifest.exists():
        return {"overlays": [], "disclaimer": OVERLAY_DISCLAIMER}
    items = json.loads(manifest.read_text())
    for it in items:
        it["url"] = f"/mvum/{it['key']}.png"
    return {"overlays": items, "disclaimer": OVERLAY_DISCLAIMER}


@app.get("/mvum/{key}.png")
async def mvum_image(key: str, request: Request):
    """Serve a registered MVUM raster PNG (display-only)."""
    if not re.fullmatch(r"[a-z0-9_]+", key):
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    p = Path(request.app.state.settings.data_dir) / "mvum" / f"{key}.png"
    if not p.exists():
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return Response(
        content=p.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
    if req.kind not in ("site", "campground", "dispersed"):
        raise ValidationError("kind must be 'site', 'campground', or 'dispersed'")
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


# --- collections (multi-membership tags over saved places — TLC-3) ----------

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")


def _check_collection_name(name: str | None) -> None:
    if not name or not name.strip():
        raise ValidationError("name is required")


def _check_color(color: str | None) -> None:
    if color is not None and not _HEX_COLOR.match(color):
        raise ValidationError("color must be a hex color like '#5b9c5a'")


@app.get("/collections")
async def list_collections(request: Request) -> dict:
    """The user's collections (each with a member count), ordered by position."""
    store: Store = request.app.state.store
    return {"collections": await store.list_collections(), "disclaimer": DISCLAIMER}


@app.post("/collections")
async def create_collection(req: CollectionRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    _check_collection_name(req.name)
    _check_color(req.color)
    col = await store.add_collection(req.name.strip(), req.color)
    return JSONResponse(status_code=201, content=col)


@app.patch("/collections/{collection_id}")
async def update_collection(
    collection_id: int, req: CollectionUpdateRequest, request: Request
) -> JSONResponse:
    store: Store = request.app.state.store
    sent = req.model_fields_set
    if "name" in sent:
        _check_collection_name(req.name)
    if "color" in sent:
        _check_color(req.color)
    col = await store.update_collection(
        collection_id,
        name=req.name.strip() if "name" in sent else UNSET,
        color=req.color if "color" in sent else UNSET,
        position=req.position if "position" in sent else UNSET,
    )
    if col is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return JSONResponse(status_code=200, content=col)


@app.delete("/collections/{collection_id}")
async def delete_collection(collection_id: int, request: Request) -> dict:
    """Delete a collection (its memberships cascade away; saved places stay)."""
    store: Store = request.app.state.store
    return {"deleted": await store.delete_collection(collection_id)}


@app.put("/saved/{saved_id}/collections")
async def set_saved_collections(
    saved_id: int, req: PlaceCollectionsRequest, request: Request
) -> JSONResponse:
    """Replace a saved place's full collection set (the per-place tag editor).
    Unknown collection ids are ignored. Returns the hydrated saved place."""
    store: Store = request.app.state.store
    place = await store.set_place_collections(saved_id, req.collection_ids)
    if place is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return JSONResponse(status_code=200, content=place)


@app.post("/collections/{collection_id}/places")
async def add_collection_places(
    collection_id: int, req: BulkPlacesRequest, request: Request
) -> JSONResponse:
    """Bulk-add a collection tag to many saved places (multi-select). Returns
    the count of new memberships; unknown saved ids are skipped."""
    store: Store = request.app.state.store
    added = await store.add_places_to_collection(collection_id, req.saved_ids)
    if added is None:
        return JSONResponse(
            status_code=404, content={"error": "NotFound", "detail": "collection not found"}
        )
    return JSONResponse(status_code=200, content={"added": added})


# --- trips (a dated grouping of saved places — user data + display, TLC-3) --


def _check_dates(start: str | None, end: str | None) -> None:
    import datetime as _dt

    for d in (start, end):
        if d is not None:
            try:
                _dt.date.fromisoformat(d)
            except ValueError:
                raise ValidationError("dates must be ISO format YYYY-MM-DD") from None
    # ISO date strings compare chronologically, so a plain string compare works.
    if start and end and start > end:
        raise ValidationError("start_date must be on or before end_date")


def _check_role(role: str) -> None:
    if role not in ("primary", "fallback"):
        raise ValidationError("role must be 'primary' or 'fallback'")


@app.get("/trips")
async def list_trips(request: Request) -> dict:
    """The user's trips (each with a member_count), newest first."""
    store: Store = request.app.state.store
    return {"trips": await store.list_trips(), "disclaimer": DISCLAIMER}


@app.post("/trips")
async def create_trip(req: TripRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    if not req.name or not req.name.strip():
        raise ValidationError("name is required")
    _check_dates(req.start_date, req.end_date)
    row = await store.add_trip(req.name.strip(), req.start_date, req.end_date, req.note)
    return JSONResponse(status_code=201, content=row)


@app.get("/trips/{trip_id}")
async def get_trip(trip_id: int, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    trip = await store.get_trip(trip_id)
    if trip is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return JSONResponse(status_code=200, content={**trip, "disclaimer": DISCLAIMER})


@app.patch("/trips/{trip_id}")
async def update_trip(trip_id: int, req: TripUpdateRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    existing = await store.get_trip(trip_id)
    if existing is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    sent = req.model_fields_set
    if "name" in sent and (not req.name or not req.name.strip()):
        raise ValidationError("name cannot be empty")
    # Validate against the effective (post-update) date pair.
    start = req.start_date if "start_date" in sent else existing["start_date"]
    end = req.end_date if "end_date" in sent else existing["end_date"]
    _check_dates(start, end)
    row = await store.update_trip(
        trip_id,
        name=req.name.strip() if "name" in sent else UNSET,
        start_date=req.start_date if "start_date" in sent else UNSET,
        end_date=req.end_date if "end_date" in sent else UNSET,
        note=req.note if "note" in sent else UNSET,
    )
    return JSONResponse(status_code=200, content={**row, "disclaimer": DISCLAIMER})


@app.delete("/trips/{trip_id}")
async def delete_trip(trip_id: int, request: Request) -> dict:
    store: Store = request.app.state.store
    return {"deleted": await store.delete_trip(trip_id)}


@app.post("/trips/{trip_id}/members")
async def add_trip_member(trip_id: int, req: MemberRequest, request: Request) -> JSONResponse:
    store: Store = request.app.state.store
    role = req.role or "primary"
    _check_role(role)
    trip = await store.add_member(trip_id, req.saved_id, role)
    if trip is None:
        return JSONResponse(
            status_code=404,
            content={"error": "NotFound", "detail": "trip or saved place not found"},
        )
    return JSONResponse(status_code=201, content={**trip, "disclaimer": DISCLAIMER})


@app.delete("/trips/{trip_id}/members/{saved_id}")
async def remove_trip_member(trip_id: int, saved_id: int, request: Request) -> dict:
    store: Store = request.app.state.store
    return {"removed": await store.remove_member(trip_id, saved_id)}


def _export_response(trip: dict, fmt: str) -> Response:
    """A downloadable GPX/KML attachment for a trip (the bridge to onX/Gaia/
    Google — TLC-3, never scoring)."""
    if fmt == "gpx":
        body, media = trip_to_gpx(trip), "application/gpx+xml"
    else:
        body, media = trip_to_kml(trip), "application/vnd.google-earth.kml+xml"
    filename = f"{trip_slug(trip)}.{fmt}"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/trips/{trip_id}/export.gpx")
async def export_trip_gpx(trip_id: int, request: Request):
    store: Store = request.app.state.store
    trip = await store.get_trip(trip_id)
    if trip is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return _export_response(trip, "gpx")


@app.get("/trips/{trip_id}/export.kml")
async def export_trip_kml(trip_id: int, request: Request):
    store: Store = request.app.state.store
    trip = await store.get_trip(trip_id)
    if trip is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return _export_response(trip, "kml")


@app.get("/trips/{trip_id}/availability")
async def trip_availability_endpoint(trip_id: int, request: Request) -> JSONResponse:
    """Live campground availability for a trip's dates (Phase 2, display-only —
    TLC-3). Per campground-kind member (any role): "N of M sites open" for
    federal (recreation.gov), else "unsupported" (the UI links out). Best-effort
    + degradable — fetched lazily by the UI so the trip renders without it; a
    source outage just drops the badges (RIDB/MVUM posture)."""
    store: Store = request.app.state.store
    trip = await store.get_trip(trip_id)
    if trip is None:
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    client: RecreationGovClient | None = request.app.state.availability
    result = await trip_availability(
        trip,
        client,
        usedirect_client=request.app.state.usedirect,
        usedirect_map=request.app.state.usedirect_map,
    )
    return JSONResponse(status_code=200, content={**result, "disclaimer": DISCLAIMER})


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


def _asset_token() -> str:
    """A short content hash of the UI bundle, computed once at startup. Used to
    version the app.js/app.css URLs so a deploy always busts the browser cache —
    iOS Safari in particular can keep serving a stale app.js even past a
    no-cache + ETag revalidation, leaving the UI half-broken."""
    import hashlib

    h = hashlib.md5()
    for name in ("app.js", "app.css"):
        p = _WEB_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:8]


_ASSET_TOKEN = _asset_token()


if (_WEB_DIR / "index.html").exists():

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        """Serve index.html with cache-busted asset URLs (?v=<hash>). The hash
        changes whenever app.js/app.css change, so the browser fetches the new
        bundle on the next load — no manual hard-refresh needed."""
        html = (_WEB_DIR / "index.html").read_text()
        html = html.replace("/app.js", f"/app.js?v={_ASSET_TOKEN}")
        html = html.replace("/app.css", f"/app.css?v={_ASSET_TOKEN}")
        return Response(
            content=html, media_type="text/html", headers={"Cache-Control": "no-store"}
        )

    # Mounted after the index route so "/" is served (versioned) by the route
    # above; everything else (app.js, app.css, …) falls through to StaticFiles.
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
