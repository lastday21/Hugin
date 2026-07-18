from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import ApplicationEventModel, ApplicationModel, VacancyModel
from hugin.domain import (
    ApplicationEventType,
    ApplicationState,
    DuplicateApplicationError,
    VacancyData,
)
from hugin.repositories import ApplicationRepository, VacancyRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    selected = Settings(environment="test", data_dir=tmp_path)
    upgrade_database(selected)
    return selected


def test_vacancy_upsert_preserves_identity_and_updates_data(settings: Settings) -> None:
    database = create_database(settings)
    published_at = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)

    try:
        with database.sessions.begin() as session:
            repository = VacancyRepository(session)
            created = repository.upsert(
                VacancyData(
                    hh_id="123",
                    title="Python developer",
                    source_url="https://hh.ru/vacancy/123",
                    employer_name="Example",
                    published_at=published_at,
                )
            )
            updated = repository.upsert(
                VacancyData(
                    hh_id="123",
                    title="Senior Python developer",
                    source_url="https://hh.ru/vacancy/123",
                    employer_name="Example",
                    published_at=published_at,
                )
            )

            assert updated.id == created.id
            assert updated.title == "Senior Python developer"
            assert repository.get_by_hh_id("123") == updated
            assert repository.get_by_hh_id("missing") is None
    finally:
        database.close()


def test_apply_intent_is_atomic_and_unique_per_vacancy(settings: Settings) -> None:
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="456",
                    title="Backend developer",
                    source_url="https://hh.ru/vacancy/456",
                )
            )
            repository = ApplicationRepository(session)
            application = repository.create_apply_intent(vacancy.id, "resume-1")

            assert application.state is ApplicationState.APPLYING
            assert repository.get_by_vacancy_id(vacancy.id) == application
            assert repository.get_by_vacancy_id(-1) is None
            events = repository.list_events(application.id)
            assert [event.event_type for event in events] == [ApplicationEventType.APPLY_INTENT]
            assert events[0].payload == {"resume_hh_id": "resume-1"}

            with pytest.raises(DuplicateApplicationError) as error:
                repository.create_apply_intent(vacancy.id, "resume-2")

            assert error.value.vacancy_id == vacancy.id
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 1
            assert session.scalar(select(func.count()).select_from(ApplicationEventModel)) == 1

        with database.sessions() as session:
            session.add(
                ApplicationModel(
                    vacancy_id=vacancy.id,
                    resume_hh_id="resume-2",
                    state=ApplicationState.APPLYING,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
    finally:
        database.close()


def test_deleting_vacancy_removes_local_application_history(settings: Settings) -> None:
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="789",
                    title="Automation developer",
                    source_url="https://hh.ru/vacancy/789",
                )
            )
            ApplicationRepository(session).create_apply_intent(vacancy.id, "resume-1")

        with database.sessions.begin() as session:
            model = session.scalar(select(VacancyModel).where(VacancyModel.id == vacancy.id))
            assert model is not None
            session.delete(model)

        with database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(VacancyModel)) == 0
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 0
            assert session.scalar(select(func.count()).select_from(ApplicationEventModel)) == 0
    finally:
        database.close()
