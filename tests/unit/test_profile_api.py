from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.repositories import AccountRepository, ResumeRepository
from tests.unit.test_resume_documents import write_resume

pytestmark = pytest.mark.integration


def test_profile_api_previews_imports_and_reviews_resume(
    settings: Settings,
    tmp_path: Path,
) -> None:
    local_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
    source = tmp_path / "Резюме ИТ.docx"
    write_resume(source)
    upgrade_database(local_settings)
    database = create_database(local_settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Тимур", "profile-api-account")
            ResumeRepository(session).upsert(
                account.id,
                "resume-profile-api",
                "Python backend разработчик",
            )
            account_id = account.id
    finally:
        database.close()

    app = create_app(local_settings)

    async def scenario() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            empty = await client.get(f"/api/profile?account_id={account_id}")
            assert empty.status_code == 200
            assert empty.json()["active_resume"] is None

            denied = await client.post(
                f"/api/profile/resume/preview?account_id={account_id}",
                files={
                    "file": (
                        source.name,
                        source.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert denied.status_code == 403

            session_key = (await client.get("/api/session")).json()["key"]
            headers = {"X-Hugin-Session": session_key}
            preview = await client.post(
                f"/api/profile/resume/preview?account_id={account_id}",
                headers=headers,
                files={
                    "file": (
                        source.name,
                        source.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert preview.status_code == 200
            preview_data = preview.json()
            assert preview_data["title"] == "Python backend разработчик"
            assert preview_data["source_type"] == "DOCX"
            assert preview_data["facts"]
            assert preview_data["questions"]

            imported = await client.post(
                f"/api/profile/resume/import?account_id={account_id}",
                headers=headers,
                json={"token": preview_data["token"]},
            )
            assert imported.status_code == 200
            profile = imported.json()
            assert profile["active_resume"]["title"] == "Python backend разработчик"
            assert profile["active_resume"]["source_original_name"] == source.name
            pending_facts = [fact for fact in profile["facts"] if fact["state"] == "PENDING"]
            assert len(pending_facts) >= 2
            assert pending_facts[0]["source_type"] == "resume"
            assert pending_facts[0]["source_reference"]
            assert pending_facts[0]["created_at"]
            assert pending_facts[0]["updated_at"]

            confirmed = await client.post(
                f"/api/profile/facts/{pending_facts[0]['id']}/confirm?account_id={account_id}",
                headers=headers,
                json={
                    "allow_in_letters": True,
                    "allow_in_forms": False,
                    "allow_in_messages": False,
                },
            )
            assert confirmed.status_code == 200
            confirmed_fact = next(
                fact for fact in confirmed.json()["facts"] if fact["id"] == pending_facts[0]["id"]
            )
            assert confirmed_fact["state"] == "CONFIRMED"
            assert confirmed_fact["allow_in_letters"] is True
            assert confirmed_fact["allow_in_forms"] is False

            rejected = await client.post(
                f"/api/profile/facts/{pending_facts[1]['id']}/reject?account_id={account_id}",
                headers=headers,
            )
            assert rejected.status_code == 200
            rejected_fact = next(
                fact for fact in rejected.json()["facts"] if fact["id"] == pending_facts[1]["id"]
            )
            assert rejected_fact["state"] == "REJECTED"

            corrected = await client.put(
                f"/api/profile/facts/{rejected_fact['id']}?account_id={account_id}",
                headers=headers,
                json={
                    "content": "Исправленное подтверждённое сведение",
                    "allow_in_letters": True,
                    "allow_in_forms": True,
                    "allow_in_messages": False,
                },
            )
            assert corrected.status_code == 200
            corrected_facts = corrected.json()["facts"]
            corrected_fact = next(
                fact
                for fact in corrected_facts
                if fact["content"] == "Исправленное подтверждённое сведение"
            )
            assert corrected_fact["id"] != rejected_fact["id"]
            assert corrected_fact["state"] == "CONFIRMED"
            assert corrected_fact["source_type"] == "user"
            assert corrected_fact["source_reference"] == f"profile-fact:{rejected_fact['id']}"
            assert corrected_fact["allow_in_letters"] is True
            assert corrected_fact["allow_in_forms"] is True
            assert corrected_fact["allow_in_messages"] is False
            assert (
                next(fact for fact in corrected_facts if fact["id"] == rejected_fact["id"])["state"]
                == "REJECTED"
            )

            fact_count = len(corrected_facts)
            reactivated = await client.put(
                f"/api/profile/facts/{corrected_fact['id']}?account_id={account_id}",
                headers=headers,
                json={
                    "content": " Исправленное подтверждённое сведение ",
                    "allow_in_letters": False,
                    "allow_in_forms": True,
                    "allow_in_messages": True,
                },
            )
            assert reactivated.status_code == 200
            assert len(reactivated.json()["facts"]) == fact_count
            reactivated_fact = next(
                fact for fact in reactivated.json()["facts"] if fact["id"] == corrected_fact["id"]
            )
            assert reactivated_fact["state"] == "CONFIRMED"
            assert reactivated_fact["allow_in_letters"] is False
            assert reactivated_fact["allow_in_messages"] is True

            invalid_correction = await client.put(
                f"/api/profile/facts/{corrected_fact['id']}?account_id={account_id}",
                headers=headers,
                json={
                    "content": "   ",
                    "allow_in_letters": False,
                    "allow_in_forms": False,
                    "allow_in_messages": False,
                },
            )
            assert invalid_correction.status_code == 422

            pending_questions = [
                question
                for question in reactivated.json()["questions"]
                if question["state"] == "PENDING"
            ]
            answered = await client.put(
                f"/api/profile/questions/{pending_questions[0]['key']}?account_id={account_id}",
                headers=headers,
                json={"answer": "Подтверждённый ответ пользователя"},
            )
            assert answered.status_code == 200
            assert answered.json()["answers"][0]["answer"] == "Подтверждённый ответ пользователя"

            dismissed = await client.post(
                (
                    f"/api/profile/questions/{pending_questions[1]['key']}/dismiss"
                    f"?account_id={account_id}"
                ),
                headers=headers,
            )
            assert dismissed.status_code == 200
            dismissed_question = next(
                question
                for question in dismissed.json()["questions"]
                if question["key"] == pending_questions[1]["key"]
            )
            assert dismissed_question["state"] == "DISMISSED"

            expired = await client.post(
                f"/api/profile/resume/import?account_id={account_id}",
                headers=headers,
                json={"token": preview_data["token"]},
            )
            assert expired.status_code == 404

    try:
        asyncio.run(scenario())
        uploads = local_settings.data_dir / "uploads"
        assert not uploads.exists() or not tuple(uploads.iterdir())
    finally:
        app.state.database.close()
