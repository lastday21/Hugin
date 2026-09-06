from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import DevelopmentAssessmentModel, DevelopmentDirectionModel
from hugin.domain.development import QualityLevel

pytestmark = pytest.mark.integration


def seed_development_direction(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            session.add(
                DevelopmentDirectionModel(
                    key="quality-check",
                    block_key="quality",
                    block_name="Качество",
                    block_position=1,
                    name="Проверка качества",
                    position=1,
                    rule="Результат подтверждён воспроизводимой проверкой.",
                    metric="Доля успешно пройденных проверок",
                    criticality=QualityLevel.HIGH,
                )
            )
            session.flush()
            session.add(
                DevelopmentAssessmentModel(
                    direction_key="quality-check",
                    score=3.0,
                    confidence=QualityLevel.MEDIUM,
                    evidence="Исходная проверка выполнена.",
                    next_step="Повторить проверку после изменения.",
                    author="Тест",
                )
            )
    finally:
        database.close()


def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
) -> Response:
    async def send() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(send())


def test_development_dashboard_persists_assessments_and_work(settings: Settings) -> None:
    seed_development_direction(settings)
    app = create_app(settings)

    first = request(app, "GET", "/api/development")
    assert first.status_code == 200
    original = first.json()
    assert len(original["blocks"]) == 1
    assert sum(len(block["directions"]) for block in original["blocks"]) == 1
    assert original["items"] == []
    assert {block["key"] for block in original["blocks"]} == {"quality"}

    second = request(app, "GET", "/api/development")
    assert second.status_code == 200
    assert len(second.json()["items"]) == len(original["items"])

    denied = request(
        app,
        "POST",
        "/api/development/directions/quality-check/assessments",
        json={
            "score": 4.2,
            "confidence": "HIGH",
            "evidence": "Проверено на размеченной выборке.",
            "next_step": "Сопоставить результаты приглашений.",
        },
    )
    assert denied.status_code == 403

    session_key = request(app, "GET", "/api/session").json()["key"]
    headers = {"X-Hugin-Session": session_key}
    assessed = request(
        app,
        "POST",
        "/api/development/directions/quality-check/assessments",
        headers=headers,
        json={
            "score": 4.2,
            "confidence": "HIGH",
            "evidence": "Проверено на размеченной выборке.",
            "next_step": "Сопоставить результаты приглашений.",
        },
    )
    assert assessed.status_code == 200
    quality_check = next(
        direction
        for block in assessed.json()["blocks"]
        for direction in block["directions"]
        if direction["key"] == "quality-check"
    )
    assert quality_check["current_assessment"]["score"] == 4.2
    assert quality_check["current_assessment"]["confidence"] == "HIGH"
    assert quality_check["assessment_count"] == 2

    created = request(
        app,
        "POST",
        "/api/development/items",
        headers=headers,
        json={
            "kind": "HYPOTHESIS",
            "title": "Проверить короткое начало письма",
            "direction_key": "quality-check",
            "status": "PLANNED",
            "priority": "MEDIUM",
            "expected_metric": "Доля писем без замечаний ручной проверки",
            "evidence": "",
            "verification_method": "Сравнить десять пар писем",
            "next_step": "Подготовить пары",
            "actual_result": "",
            "reference_codes": ["TASK-001"],
            "author": "Тест",
        },
    )
    assert created.status_code == 200
    item = next(
        item
        for item in created.json()["items"]
        if item["title"] == "Проверить короткое начало письма"
    )
    assert item["status"] == "PLANNED"

    item_id = item["id"]
    item["status"] = "DONE"
    item["actual_result"] = "Пары подготовлены и проверены."
    item["author"] = "Тест"
    item.pop("id")
    item.pop("external_key")
    item.pop("created_at")
    item.pop("updated_at")
    updated = request(
        app,
        "PUT",
        f"/api/development/items/{item_id}",
        headers=headers,
        json=item,
    )
    assert updated.status_code == 200
    assert (
        next(
            row
            for row in updated.json()["items"]
            if row["title"] == "Проверить короткое начало письма"
        )["status"]
        == "DONE"
    )
