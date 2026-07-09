"""FastAPI application factory for the PIM read API.

Wires CORS, the ``/fetch_pims_records`` routes, and an unauthenticated
``/health`` readiness probe. The same ``app`` runs locally under ``uvicorn`` and
on Lambda via :mod:`pim_whitelist.api.handler`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import routes
from .deps import get_index, get_settings
from .index import PimIndex


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="PIMS Whitelist API",
        version="1.0.0",
        description=(
            "Read-only access to the curated NASA SMD PIM (Platforms / "
            "Instruments / Missions) whitelists, with optional division "
            "filtering and pagination."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(routes.router)

    @app.get("/health", tags=["meta"], summary="Readiness probe (no auth)")
    def health(
        index: Annotated[PimIndex, Depends(get_index)],
    ) -> JSONResponse:
        """Structured readiness check.

        Returns 200 only when every dataset loaded at least one record; 503 with
        the per-type counts otherwise, so a broken artifact fails a deploy gate
        fast rather than serving an empty index.
        """
        counts = index.counts()
        ready = index.is_ready()
        body = {
            "status": "ok" if ready else "unhealthy",
            "data_version": index.data_version,
            "counts": counts,
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


app = create_app()
