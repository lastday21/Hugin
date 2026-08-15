from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    DirectionVacancyModel,
    VacancyModel,
)
from hugin.domain import (
    ApplicationEventType,
    ApplicationState,
    DuplicateApplicationError,
    TaskState,
    VacancyAvailability,
    VacancyData,
    VacancyState,
)
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    ResumeRepository,
    VacancyRepository,
)
from hugin.repositories.tasks import FORM_PREFLIGHT_RUNNING

pytestmark = pytest.mark.integration


def test_vacancy_upsert_preserves_identity_and_updates_data(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    published_at = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    details_fetched_at = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)

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
                    description="Python backend service",
                    experience="1-3 года",
                    key_skills=("Python", "FastAPI"),
                    details_fetched_at=details_fetched_at,
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
            assert updated.description == "Python backend service"
            assert updated.key_skills == ("Python", "FastAPI")
            assert updated.details_fetched_at == details_fetched_at
            assert repository.get_by_hh_id("123") == updated
            assert repository.get_by_hh_id("missing") is None
    finally:
        database.close()


def test_search_card_does_not_reopen_unavailable_vacancy(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            repository = VacancyRepository(session)
            vacancy = repository.upsert(
                VacancyData(
                    "closed-123",
                    "Python developer",
                    "https://hh.ru/vacancy/closed-123",
                    description="Сохранённое описание",
                    key_skills=("Python",),
                    details_fetched_at=datetime(2026, 7, 30, tzinfo=UTC),
                )
            )
            repository.mark_unavailable(vacancy.id, VacancyAvailability.ARCHIVED)
            repeated = repository.upsert(
                VacancyData(
                    "closed-123",
                    "Python developer",
                    "https://hh.ru/vacancy/closed-123",
                )
            )

            assert repeated.availability is VacancyAvailability.ARCHIVED
            assert repeated.description == "Сохранённое описание"
            assert repeated.key_skills == ("Python",)
    finally:
        database.close()


def test_unavailable_vacancy_closes_waiting_application_and_task(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            resume = ResumeRepository(session).upsert(account.id, "resume-closed", "Developer")
            direction = DirectionRepository(session).create(account.id, "Backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="closed-queue",
                    title="Python developer",
                    source_url="https://hh.ru/vacancy/closed-queue",
                )
            )
            DirectionRepository(session).track_vacancy(direction.id, vacancy.id)
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = ApplicationTaskModel(
                application_id=application.id,
                state=TaskState.PENDING,
                priority_score=80,
            )
            session.add(task)
            session.flush()

            VacancyRepository(session).mark_unavailable(
                vacancy.id,
                VacancyAvailability.CLOSED,
            )

            stored_application = session.get(ApplicationModel, application.id)
            stored_task = session.get(ApplicationTaskModel, task.id)
            tracked = session.scalar(
                select(DirectionVacancyModel).where(
                    DirectionVacancyModel.direction_id == direction.id,
                    DirectionVacancyModel.vacancy_id == vacancy.id,
                )
            )
            assert stored_application is not None
            assert stored_application.state is ApplicationState.CLOSED
            assert stored_task is not None
            assert stored_task.state is TaskState.SKIPPED
            assert stored_task.last_error_code == "VACANCY_CLOSED"
            assert tracked is not None
            assert tracked.state is VacancyState.CLOSED
            events = ApplicationRepository(session).list_events(application.id)
            assert events[-1].event_type is ApplicationEventType.STATE_CHANGED
            assert events[-1].payload["reason"] == "VACANCY_CLOSED"
    finally:
        database.close()


def test_unavailable_vacancy_closes_running_preflight_but_not_running_submit(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Preflight account")
            first_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-preflight-closed",
                "Developer",
            )
            second_resume = ResumeRepository(session).upsert(
                account.id,
                "resume-submit-running",
                "Developer second",
            )
            direction = DirectionRepository(session).create(account.id, "Backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    hh_id="closed-during-preflight",
                    title="Python developer",
                    source_url="https://hh.ru/vacancy/closed-during-preflight",
                )
            )
            DirectionRepository(session).track_vacancy(direction.id, vacancy.id)
            preflight_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                first_resume.id,
                direction.id,
            )
            submit_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                second_resume.id,
                direction.id,
            )
            preflight_task = ApplicationTaskModel(
                application_id=preflight_application.id,
                state=TaskState.RUNNING,
                priority_score=80,
                last_error_code=FORM_PREFLIGHT_RUNNING,
            )
            submit_task = ApplicationTaskModel(
                application_id=submit_application.id,
                state=TaskState.RUNNING,
                priority_score=79,
            )
            session.add_all((preflight_task, submit_task))
            session.flush()

            VacancyRepository(session).mark_unavailable(
                vacancy.id,
                VacancyAvailability.CLOSED,
            )

            stored_preflight_application = session.get(
                ApplicationModel,
                preflight_application.id,
            )
            stored_preflight_task = session.get(ApplicationTaskModel, preflight_task.id)
            stored_submit_application = session.get(
                ApplicationModel,
                submit_application.id,
            )
            stored_submit_task = session.get(ApplicationTaskModel, submit_task.id)
            assert stored_preflight_application is not None
            assert stored_preflight_application.state is ApplicationState.CLOSED
            assert stored_preflight_task is not None
            assert stored_preflight_task.state is TaskState.SKIPPED
            assert stored_preflight_task.last_error_code == "VACANCY_CLOSED"
            assert stored_submit_application is not None
            assert stored_submit_application.state is ApplicationState.APPLYING
            assert stored_submit_task is not None
            assert stored_submit_task.state is TaskState.RUNNING
    finally:
        database.close()


def test_detail_refresh_prioritizes_ready_tasks_and_skips_finished_work(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            resume = ResumeRepository(session).upsert(account.id, "resume-refresh", "Developer")
            direction = DirectionRepository(session).create(account.id, "Backend")
            repository = VacancyRepository(session)
            directions = DirectionRepository(session)

            def add_vacancy(
                hh_id: str,
                *,
                details_fetched_at: datetime | None,
            ) -> int:
                vacancy = repository.upsert(
                    VacancyData(
                        hh_id=hh_id,
                        title="Python developer",
                        source_url=f"https://hh.ru/vacancy/{hh_id}",
                        description=(
                            "Разработка на Python" if details_fetched_at is not None else None
                        ),
                        details_fetched_at=details_fetched_at,
                    )
                )
                directions.track_vacancy(direction.id, vacancy.id)
                return vacancy.id

            ready_never_id = add_vacancy("ready-never", details_fetched_at=None)
            ready_old_id = add_vacancy(
                "ready-old",
                details_fetched_at=now - timedelta(days=4),
            )
            old_unassigned_id = add_vacancy(
                "old-unassigned",
                details_fetched_at=now - timedelta(days=3),
            )
            new_unassigned_id = add_vacancy("new-unassigned", details_fetched_at=None)
            finished_id = add_vacancy(
                "finished-old",
                details_fetched_at=now - timedelta(days=10),
            )

            applications = ApplicationRepository(session)
            ready_never = applications.create_apply_intent(
                account.id,
                ready_never_id,
                resume.id,
                direction.id,
            )
            ready_old = applications.create_apply_intent(
                account.id,
                ready_old_id,
                resume.id,
                direction.id,
            )
            finished = applications.create_apply_intent(
                account.id,
                finished_id,
                resume.id,
                direction.id,
            )
            session.add_all(
                (
                    ApplicationTaskModel(
                        application_id=ready_never.id,
                        state=TaskState.PENDING,
                        priority_score=80,
                    ),
                    ApplicationTaskModel(
                        application_id=ready_old.id,
                        state=TaskState.RETRY_SCHEDULED,
                        priority_score=80,
                    ),
                    ApplicationTaskModel(
                        application_id=finished.id,
                        state=TaskState.COMPLETED,
                        priority_score=80,
                    ),
                )
            )
            finished_model = session.get(ApplicationModel, finished.id)
            assert finished_model is not None
            finished_model.state = ApplicationState.APPLIED
            session.flush()

            pending = repository.list_pending_for_direction(direction.id, limit=10)

            assert [vacancy.hh_id for vacancy in pending] == [
                "ready-never",
                "ready-old",
                "new-unassigned",
                "old-unassigned",
            ]
            assert finished_id not in {vacancy.id for vacancy in pending}
            assert old_unassigned_id in {vacancy.id for vacancy in pending}
            assert new_unassigned_id in {vacancy.id for vacancy in pending}
    finally:
        database.close()


def test_detail_refresh_prioritizes_new_unfetched_vacancies_after_ready_tasks(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Main account")
            resume = ResumeRepository(session).upsert(account.id, "resume-refresh", "Developer")
            direction = DirectionRepository(session).create(account.id, "Backend")
            vacancies = VacancyRepository(session)
            directions = DirectionRepository(session)

            def add_vacancy(hh_id: str, *, created_at: datetime) -> int:
                vacancy = vacancies.upsert(
                    VacancyData(
                        hh_id=hh_id,
                        title="Python developer",
                        source_url=f"https://hh.ru/vacancy/{hh_id}",
                    )
                )
                directions.track_vacancy(direction.id, vacancy.id)
                model = session.get(VacancyModel, vacancy.id)
                assert model is not None
                model.created_at = created_at
                session.flush()
                return vacancy.id

            ready_id = add_vacancy("ready", created_at=now - timedelta(days=5))
            add_vacancy("old-unfetched", created_at=now - timedelta(days=4))
            add_vacancy("new-unfetched", created_at=now)

            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                ready_id,
                resume.id,
                direction.id,
            )
            session.add(
                ApplicationTaskModel(
                    application_id=application.id,
                    state=TaskState.PENDING,
                    priority_score=80,
                )
            )
            session.flush()

            pending = vacancies.list_pending_for_direction(direction.id, limit=10)

            assert [vacancy.hh_id for vacancy in pending] == [
                "ready",
                "new-unfetched",
                "old-unfetched",
            ]
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
