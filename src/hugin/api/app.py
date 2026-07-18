from __future__ import annotations

from fastapi import FastAPI

from hugin import __version__
from hugin.api.routes.health import router as health_router
from hugin.core.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
    )
    application.state.settings = resolved_settings
    application.include_router(health_router)
    return application
