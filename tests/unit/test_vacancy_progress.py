from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    ScreeningFormModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.content import (
    CoverLetterState,
    ScreeningFormState,
    cover_letter_instruction_version,
)
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.directions import AccountRepository, ResumeRepository
from hugin.services.ai_prompts import DEFAULT_AI_PROMPTS
from hugin.services.vacancy_analysis import RULES_VERSION
from hugin.services.vacancy_progress import VacancyProgressService

pytestmark = pytest.mark.integration
START = datetime(2026, 9, 10, 19, tzinfo=UTC)


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    db = create_database(settings)
    try:
        with db.sessions.begin() as session:
            yield session
    finally:
        db.close()


def setup(session: Session) -> tuple[int, CareerDirectionModel, int]:
    account = AccountRepository(session).create("Проверка", "progress")
    resume = ResumeRepository(session).upsert(account.id, "resume-progress", "Python")
    direction = CareerDirectionModel(account_id=account.id, name="Python")
    session.add(direction)
    session.flush()
    return account.id, direction, resume.id


def vacancy(
    session: Session,
    direction: CareerDirectionModel,
    key: str,
    category: str | None = "MATCH",
    read_at: datetime | None = START,
) -> VacancyModel:
    row = VacancyModel(
        hh_id=key,
        title=f"Вакансия {key}",
        source_url=f"https://hh.ru/{key}",
        details_fetched_at=read_at,
        description="Задачи Python",
    )
    session.add(row)
    session.flush()
    session.add(
        DirectionVacancyModel(
            vacancy_id=row.id,
            direction_id=direction.id,
            state=VacancyState.ANALYZED,
            rules_version=RULES_VERSION if category is not None else None,
            rules_details={"category": category},
        )
    )
    session.flush()
    return row


def task(
    session: Session,
    account: int,
    direction: CareerDirectionModel,
    resume: int,
    row: VacancyModel,
    state: TaskState = TaskState.PENDING,
    letter: bool = False,
) -> ApplicationTaskModel:
    app = ApplicationModel(
        account_id=account, direction_id=direction.id, resume_id=resume, vacancy_id=row.id
    )
    session.add(app)
    session.flush()
    queued = ApplicationTaskModel(
        application_id=app.id, state=state, priority_score=80, scheduled_at=START
    )
    session.add(queued)
    if letter:
        session.add(
            CoverLetterModel(
                application_id=app.id,
                vacancy_id=row.id,
                resume_id=resume,
                direction_id=direction.id,
                text="Здравствуйте! Разрабатываю на Python.",
                model_name="test",
                instruction_version=cover_letter_instruction_version(
                    DEFAULT_AI_PROMPTS.cover_letter
                ),
                state=CoverLetterState.READY,
                quality_passed=True,
            )
        )
    session.flush()
    return queued


def test_progress_partitions_same_read_cohort_and_distinguishes_preparation(
    session: Session,
) -> None:
    account, direction, resume = setup(session)
    vacancy(session, direction, "pending", None)
    vacancy(session, direction, "rejected", "REJECTED")
    ready = vacancy(session, direction, "ready")
    other_direction = CareerDirectionModel(account_id=account, name="ИТ")
    session.add(other_direction)
    session.flush()
    session.add(DirectionVacancyModel(direction_id=other_direction.id, vacancy_id=ready.id))
    task(session, account, direction, resume, vacancy(session, direction, "preparing"))
    task(session, account, direction, resume, vacancy(session, direction, "prepared"), letter=True)
    sent = task(
        session,
        account,
        direction,
        resume,
        vacancy(session, direction, "sent"),
        state=TaskState.COMPLETED,
    )
    ApplicationRepository(session).transition_state(
        sent.application_id, ApplicationState.APPLIED, {"hh_status": "APPLIED"}
    )
    task(
        session,
        account,
        direction,
        resume,
        vacancy(session, direction, "unknown"),
        state=TaskState.UNKNOWN_RESULT,
        letter=True,
    )
    vacancy(session, direction, "unavailable").availability = VacancyAvailability.ARCHIVED
    vacancy(session, direction, "yesterday", read_at=START - timedelta(microseconds=1))
    vacancy(session, direction, "not-read", read_at=None)
    foreign = AccountRepository(session).create("Другой", "foreign-progress")
    foreign_direction = CareerDirectionModel(account_id=foreign.id, name="Чужое")
    session.add(foreign_direction)
    session.flush()
    vacancy(session, foreign_direction, "foreign")
    session.flush()
    result = VacancyProgressService(session, account).snapshot(START)
    assert result.total == 8
    assert {stage.key: stage.count for stage in result.stages} == {
        "awaiting_evaluation": 1,
        "rejected": 1,
        "ready": 1,
        "preparing": 1,
        "prepared": 1,
        "sent": 1,
        "review": 1,
        "unavailable": 1,
    }
    assert sum(stage.count for stage in result.stages) == result.total
    assert result.oldest_pending_at == START


@pytest.mark.parametrize(
    "state", [TaskState.UNKNOWN_RESULT, TaskState.REVIEW_REQUIRED, TaskState.INPUT_REQUIRED]
)
def test_attention_precedes_ready_letter(session: Session, state: TaskState) -> None:
    account, direction, resume = setup(session)
    task(
        session,
        account,
        direction,
        resume,
        vacancy(session, direction, "attention"),
        state,
        letter=True,
    )
    result = VacancyProgressService(session, account).snapshot(START)
    assert next(stage for stage in result.stages if stage.key == "review").count == 1
    assert next(stage for stage in result.stages if stage.key == "prepared").count == 0


@pytest.mark.parametrize("problem", ["instruction", "empty", "form", "quality", "skipped"])
def test_incomplete_or_stale_letter_is_not_prepared(session: Session, problem: str) -> None:
    from sqlalchemy import select

    account, direction, resume = setup(session)
    queued = task(
        session, account, direction, resume, vacancy(session, direction, "stale"), letter=True
    )
    letter = session.scalar(select(CoverLetterModel))
    assert letter is not None
    if problem == "instruction":
        letter.instruction_version = "obsolete"
    elif problem == "empty":
        letter.text = ""
    elif problem == "quality":
        letter.quality_passed = False
    elif problem == "skipped":
        queued.state = TaskState.SKIPPED
    else:
        session.add(
            ScreeningFormModel(
                application_id=queued.application_id,
                state=ScreeningFormState.INPUT_REQUIRED,
                version_hash="test",
            )
        )
    session.flush()
    result = VacancyProgressService(session, account).snapshot(START)
    assert next(stage for stage in result.stages if stage.key == "prepared").count == 0


def test_returning_to_previous_instruction_uses_its_ready_letter(session: Session) -> None:
    account, direction, resume = setup(session)
    row = vacancy(session, direction, "previous-instruction")
    queued = task(session, account, direction, resume, row, letter=True)
    session.add(
        CoverLetterModel(
            application_id=queued.application_id,
            vacancy_id=row.id,
            resume_id=resume,
            direction_id=direction.id,
            text="Другая инструкция",
            model_name="test",
            instruction_version="other-instruction",
            state=CoverLetterState.READY,
            quality_passed=True,
        )
    )
    session.flush()
    page = VacancyProgressService(session, account).vacancies(START, "prepared")
    assert [item.vacancy_id for item in page.items] == [row.hh_id]


def test_restored_confirmed_form_supersedes_newer_invalidated_form(session: Session) -> None:
    account, direction, resume = setup(session)
    row = vacancy(session, direction, "restored-form")
    row.has_screening_form = True
    queued = task(session, account, direction, resume, row, letter=True)
    session.add(
        ScreeningFormModel(
            application_id=queued.application_id,
            version_hash="original",
            state=ScreeningFormState.CONFIRMED,
        )
    )
    session.flush()
    session.add(
        ScreeningFormModel(
            application_id=queued.application_id,
            version_hash="invalidated",
            state=ScreeningFormState.INVALIDATED,
        )
    )
    session.flush()
    page = VacancyProgressService(session, account).vacancies(START, "prepared")
    assert [item.vacancy_id for item in page.items] == [row.hh_id]


def test_stage_pages_are_stable_unique_and_empty_is_known(session: Session) -> None:
    account, direction, _ = setup(session)
    for index in range(3):
        vacancy(session, direction, str(index), None, START + timedelta(minutes=index))
    service = VacancyProgressService(session, account)
    first = service.vacancies(START, "awaiting_evaluation", offset=0, limit=2)
    second = service.vacancies(START, "awaiting_evaluation", offset=2, limit=2)
    assert first.total == second.total == 3
    assert len(first.items) == 2 and len(second.items) == 1
    assert {item.vacancy_id for item in (*first.items, *second.items)} == {"0", "1", "2"}
    assert service.vacancies(START, "sent", offset=0, limit=2).total == 0
    with pytest.raises(ValueError):
        service.vacancies(START, "invented", offset=0, limit=2)
    assert service.snapshot(START + timedelta(days=1)).total == 0


def test_inactive_direction_does_not_claim_preparation_will_start(session: Session) -> None:
    account, direction, _ = setup(session)
    vacancy(session, direction, "inactive")
    direction.is_active = False
    session.flush()
    result = VacancyProgressService(session, account).snapshot(START)
    assert result.total == 1
    assert next(stage for stage in result.stages if stage.key == "review").count == 1
