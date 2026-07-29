from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, Request, Response

from .config import Settings, get_settings
from .logging import setup_logging
from .routes.album_routes import router as album_router
from .routes.asset_routes import router as asset_router
from .routes.auth_routes import router as auth_router
from .routes.depiction_routes import router as depiction_router
from .routes.external_routes import router as external_router
from .routes.search_routes import router as search_router
from .routes.serve_routes import router as serve_router
from .routes.tag_routes import router as tag_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    logging.getLogger(__name__).info("app_start", extra={"env": settings.environment})
    yield
    logging.getLogger(__name__).info("app_stop")


app = FastAPI(title="image-manager", lifespan=lifespan)


@app.middleware("http")
async def add_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = request.headers.get("x-request-id")
    if not request_id:
        # generate a simple UUID-like value without import overhead
        import uuid

        request_id = str(uuid.uuid4())
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.get("/healthz")
async def healthz(settings: Settings = Depends(get_settings)) -> dict[str, str]:  # noqa: B008
    return {"status": "ok", "service": settings.app_name}


app.include_router(auth_router)
app.include_router(asset_router)
app.include_router(depiction_router)
app.include_router(serve_router)
app.include_router(external_router)
app.include_router(album_router)
app.include_router(tag_router)
app.include_router(search_router)
