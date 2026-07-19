from __future__ import annotations

from datetime import UTC, datetime

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
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)

pytestmark = pytest.mark.integration


def test_vacancy_upsert_preserves_identity_and_updates_data(settings: Settings) -> None:
    upgrade_database(settings)
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


def test_apply_intent_is_unique_per_account_vacancy_and_resume(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            resumes = ResumeRepository(session)
            backend = resumes.upsert(account.id, "resume-1", "Backend developer")
            automation = resumes.upsert(account.id, "resume-2", "Automation engineer")
            direction = DirectionRepository(session).create(account.id, "Backend")
            DirectionRepository(session).attach_resume(direction.id, backend.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="456",
                    title="Python developer",
                    source_url="https://hh.ru/vacancy/456",
                )
            )
            repository = ApplicationRepository(session)
            first = repository.create_apply_intent(account.id, vacancy.id, backend.id, direction.id)
            second = repository.create_apply_intent(account.id, vacancy.id, automation.id)

            assert first.state is ApplicationState.APPLYING
            assert repository.get_by_key(account.id, vacancy.id, backend.id) == first
            assert repository.get_by_key(account.id, vacancy.id, -1) is None
            assert repository.list_by_vacancy_id(vacancy.id) == [first, second]
            events = repository.list_events(first.id)
            assert [event.event_type for event in events] == [ApplicationEventType.APPLY_INTENT]
            assert events[0].payload == {
                "account_id": account.id,
                "resume_id": backend.id,
                "direction_id": direction.id,
            }

            with pytest.raises(DuplicateApplicationError) as error:
                repository.create_apply_intent(account.id, vacancy.id, backend.id)

            assert error.value.account_id == account.id
            assert error.value.vacancy_id == vacancy.id
            assert error.value.resume_id == backend.id
            assert session.scalar(select(func.count()).select_from(ApplicationModel)) == 2
            assert session.scalar(select(func.count()).select_from(ApplicationEventModel)) == 2

            other_account = AccountRepository(session).create("Other account")
            other_resume = resumes.upsert(other_account.id, "resume-3", "Other resume")
            with pytest.raises(ValueError, match="application account"):
                repository.create_apply_intent(account.id, vacancy.id, other_resume.id)

        with database.sessions() as session:
            session.add(
                ApplicationModel(
                    account_id=account.id,
                    vacancy_id=vacancy.id,
                    resume_id=other_resume.id,
                    state=ApplicationState.APPLYING,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            session.add(
                ApplicationModel(
                    account_id=account.id,
                    vacancy_id=vacancy.id,
                    resume_id=backend.id,
                    state=ApplicationState.APPLYING,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
    finally:
        database.close()


def test_deleting_vacancy_removes_local_application_history(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Developer")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="789",
                    title="Automation developer",
                    source_url="https://hh.ru/vacancy/789",
                )
            )
            ApplicationRepository(session).create_apply_intent(account.id, vacancy.id, resume.id)

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
