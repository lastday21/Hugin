import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from hugin.api.app import create_app
from hugin.core.settings import Settings


def get(app: FastAPI, path: str) -> Response:
    async def request() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_health_returns_service_state(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))
    response = get(app, "/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "hugin",
        "version": "0.1.0",
    }


def test_openapi_is_available_outside_production(tmp_path: Path) -> None:
    app = create_app(Settings(environment="test", data_dir=tmp_path))
    response = get(app, "/openapi.json")

    assert response.status_code == 200
    assert "/health" in response.json()["paths"]


def test_documentation_is_disabled_in_production(tmp_path: Path) -> None:
    app = create_app(Settings(environment="production", data_dir=tmp_path))
    response = get(app, "/docs")

    assert response.status_code == 404
