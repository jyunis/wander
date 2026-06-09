"""Web API (FC-8).

FastAPI app. Milestone 1 ships `GET /health` (DB connectivity + SpatiaLite
load + cache-dir writability) and the static-UI mount point. Scoring endpoints
(`POST /score`, `GET /runs/{id}`, `GET /runs/{id}/geojson`, `GET /config`)
land in milestones 4–5. The API contains no scoring/geo logic — it delegates
to the engine and surfaces typed errors verbatim (TLC-10).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cairn import DISCLAIMER, __version__
from cairn.config import get_settings
from cairn.logging import configure
from cairn.store import Store

log = logging.getLogger("cairn.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure(settings.log_level)
    log.info("opening store at %s", settings.db_path)
    store = await Store.open(settings)
    app.state.store = store
    app.state.settings = settings
    log.info("cairn %s ready on %s:%s", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        await store.close()


app = FastAPI(title="Cairn", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness + readiness: SpatiaLite loads, schema present, caches writable."""
    store: Store = app.state.store
    try:
        info = await store.health()
    except Exception as exc:  # surface, never swallow (TLC-10)
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
