from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from hugin import __version__
from hugin.api.routes.communications import router as communications_router
from hugin.api.routes.health import router as health_router
from hugin.api.routes.profile import router as profile_router
from hugin.api.routes.workspace import router as workspace_router
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database


def web_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "web_dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    database = create_database(resolved_settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            database.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.session_key = secrets.token_urlsafe(32)
    application.include_router(communications_router)
    application.include_router(health_router)
    application.include_router(profile_router)
    application.include_router(workspace_router)
    assets = web_directory()
    if assets.is_dir():
        application.mount("/", StaticFiles(directory=assets, html=True), name="web")
    return application
