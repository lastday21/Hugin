from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from hugin import __version__
from hugin.api.routes.communications import router as communications_router
from hugin.api.routes.health import router as health_router
from hugin.api.routes.profile import router as profile_router
from hugin.api.routes.workspace import router as workspace_router
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database
from hugin.diagnostics import OperationJournal


def web_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "web_dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    database = create_database(resolved_settings)
    journal = OperationJournal(resolved_settings.data_dir)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        running = journal.start(
            "server",
            "lifecycle",
            action="start",
            environment=resolved_settings.environment,
        )
        running.succeed()
        try:
            yield
        finally:
            database.close()
            journal.record(
                "server",
                "lifecycle",
                status="completed",
                action="stop",
            )

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.journal = journal
    application.state.session_key = secrets.token_urlsafe(32)

    @application.middleware("http")
    async def record_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        should_record = request.method != "GET" and request.url.path.startswith("/api/")
        run = (
            journal.start(
                "server",
                "request",
                method=request.method,
                path=request.url.path,
            )
            if should_record
            else None
        )
        try:
            response = await call_next(request)
        except Exception as error:
            if run is None:
                run = journal.start(
                    "server",
                    "request",
                    method=request.method,
                    path=request.url.path,
                )
            run.fail(error)
            raise
        if run is not None:
            if response.status_code >= 400:
                run.block(http_status=response.status_code)
            else:
                run.succeed(http_status=response.status_code)
        elif response.status_code >= 400 and request.url.path.startswith("/api/"):
            journal.record(
                "server",
                "request",
                status="blocked",
                level="WARNING",
                method=request.method,
                path=request.url.path,
                http_status=response.status_code,
            )
        return response

    application.include_router(communications_router)
    application.include_router(health_router)
    application.include_router(profile_router)
    application.include_router(workspace_router)
    assets = web_directory()
    if assets.is_dir():
        application.mount("/", StaticFiles(directory=assets, html=True), name="web")
    return application
