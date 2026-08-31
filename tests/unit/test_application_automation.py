from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import delete, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationEventModel,
    CandidateProfileModel,
    CoverLetterFactModel,
    CoverLetterModel,
    DirectionVacancyModel,
    IncidentModel,
    ResumeModel,
    VerifiedFactModel,
)
from hugin.domain import (
    ApplicationEventType,
    ApplicationReconciliationResult,
    ApplicationState,
    HhApplyResult,
    HhApplyStatus,
    ReconciliationStatus,
    ScreeningFormState,
    SystemState,
    TaskState,
    VacancyAvailability,
    VacancyData,
)
from hugin.domain.content import (
    ConfirmationState,
    CoverLetterState,
    IncidentState,
    cover_letter_instruction_version,
)
from hugin.domain.directions import VacancyState
from hugin.domain.hh_sync import HhNegotiationData, HhNegotiationStatus
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
    SystemStateRepository,
    VacancyRepository,
)
from hugin.repositories.tasks import (
    FORM_PREFLIGHT_PASSED,
    FORM_PREFLIGHT_RUNNING,
    FORM_RETRY_EXHAUSTED,
)
from hugin.services import application_automation as application_automation_module
from hugin.services.ai_prompts import DEFAULT_AI_PROMPTS
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.application_reconciliation import ApplicationReconciliationService
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.cover_letter import MANUAL_REVIEW_MODEL, CoverLetterService
from hugin.services.hh_sync import HhSynchronizationService
from hugin.services.queue import QueueService
from hugin.services.vacancy_analysis import (
    RULES_VERSION,
    RuleCategory,
    VacancyAnalysisService,
)

pytestmark = pytest.mark.integration


def _supervised_letter() -> str:
    return (
        "Здравствуйте!\n\n"
        "Разрабатываю серверные приложения на Python, реализую прикладную логику и уделяю "
        "внимание обработке ошибок. При работе разделяю прикладную часть и доступ к данным, "
        "проверяю изменения автоматическими проверками и разбираю результат до понятной "
        "причины. Такой подход помогает аккуратно дорабатывать серверную часть, сохранять "
        "целостность данных и проверять поведение службы перед выпуском.\n\n"
        "Готов подробнее рассказать про выполненные задачи и обсудить задачи команды."
    )


@pytest.mark.parametrize(
    ("has_previous_submission", "expected_state", "expected_error"),
    [
        (False, TaskState.RETRY_SCHEDULED, HhApplyStatus.QUESTIONS_REQUIRED.value),
        (True, TaskState.REVIEW_REQUIRED, FORM_RETRY_EXHAUSTED),
    ],
)
def test_repeated_confirmed_form_does_not_loop_in_queue(
    monkeypatch: pytest.MonkeyPatch,
    has_previous_submission: bool,
    expected_state: TaskState,
    expected_error: str,
) -> None:
    draft = SimpleNamespace(
        state=ScreeningFormState.CONFIRMED,
        questions=(SimpleNamespace(),),
        answers=(SimpleNamespace(),),
    )

    class FakeScreeningDraftService:
        def __init__(self, session: object) -> None:
            assert session is not None

        def get_auto_submission(self, application_id: int) -> object | None:
            assert application_id == 51
            return object() if has_previous_submission else None

        def capture_questions(self, application_id: int, questions: object) -> object:
            assert application_id == 51
            assert questions == ("Расскажите об опыте",)  # noqa: RUF001
            return draft

    transitions: list[tuple[int, TaskState, dict[str, object]]] = []

    class FakeTasks:
        def transition(
            self,
            task_id: int,
            target: TaskState,
            **kwargs: object,
        ) -> object:
            transitions.append((task_id, target, kwargs))
            return object()

    monkeypatch.setattr(
        application_automation_module,
        "ScreeningDraftService",
        FakeScreeningDraftService,
    )
    service = ApplicationAutomationService(object())  # type: ignore[arg-type]
    service._tasks = FakeTasks()  # type: ignore[assignment]
    job = cast(
        ApplyJob,
        SimpleNamespace(
            task=SimpleNamespace(id=41),
            application=SimpleNamespace(id=51),
            vacancy=SimpleNamespace(source_url="https://hh.ru/vacancy/61"),
        ),
    )

    recorded = service.record_result(
        job,
        HhApplyResult(
            HhApplyStatus.QUESTIONS_REQUIRED,
            job.vacancy.source_url,
            questions=("Расскажите об опыте",),  # noqa: RUF001
        ),
        now=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )

    assert not recorded.sent
    assert not recorded.blocking
    assert len(transitions) == 1
    task_id, state, values = transitions[0]
    assert task_id == 41
    assert state is expected_state
    assert values["error_code"] == expected_error


def test_form_preflight_claims_only_task_without_current_letter(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "preflight-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "preflight-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "preflight-vacancy",
                    "Python backend",
                    "https://hh.ru/vacancy/preflight-vacancy",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=90,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 90, now)
            system = SystemStateRepository(session)
            system.transition(SystemState.RUNNING)
            system.set_next_apply_at(now + timedelta(seconds=60))
            service = ApplicationAutomationService(session)

            assert service.has_pending_application_work(
                account_id=account.id,
                include_scheduled=True,
                now=now,
            )
            job = service.claim_next_form_preflight(account_id=account.id, now=now)

            assert job is not None
            assert job.task.id == task.id
            assert job.cover_letter is None
            assert job.task.last_error_code == FORM_PREFLIGHT_RUNNING
            service.release_form_preflight(job, now=now)
            released = QueueTaskRepository(session).get(task.id)
            assert released.state is TaskState.RETRY_SCHEDULED
            assert released.last_error_code == FORM_PREFLIGHT_PASSED

            session.add(
                CoverLetterModel(
                    application_id=application.id,
                    vacancy_id=vacancy.id,
                    direction_id=direction.id,
                    resume_id=resume.id,
                    text="Здравствуйте!\n\nПроверенное письмо.",  # noqa: RUF001
                    instruction_version=cover_letter_instruction_version(
                        DEFAULT_AI_PROMPTS.cover_letter
                    ),
                    model_name=MANUAL_REVIEW_MODEL,
                    state=CoverLetterState.READY,
                )
            )
            session.flush()

            assert service.claim_next_form_preflight(account_id=account.id, now=now) is None
            assert QueueTaskRepository(session).get(task.id).state is TaskState.RETRY_SCHEDULED

            closed_vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "preflight-closed",
                    "Python backend",
                    "https://hh.ru/vacancy/preflight-closed",
                )
            )
            directions.track_vacancy(direction.id, closed_vacancy.id)
            directions.apply_rules(
                direction.id,
                closed_vacancy.id,
                state=VacancyState.QUEUED,
                score=89,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            closed_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                closed_vacancy.id,
                resume.id,
                direction.id,
            )
            closed_task = QueueTaskRepository(session).enqueue(
                closed_application.id,
                89,
                now,
            )
            closed_job = service.claim_next_form_preflight(
                account_id=account.id,
                now=now,
            )
            assert closed_job is not None

            recorded = service.record_result(
                closed_job,
                HhApplyResult(
                    HhApplyStatus.VACANCY_CLOSED,
                    closed_job.vacancy.source_url,
                ),
                now=now,
            )

            assert not recorded.sent
            assert (
                ApplicationRepository(session).get(closed_application.id).state
                is ApplicationState.CLOSED
            )
            assert QueueTaskRepository(session).get(closed_task.id).state is TaskState.SKIPPED
    finally:
        database.close()


def test_supervised_form_preflight_claims_exact_task_while_paused(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Иван",
                "supervised-preflight-account",
            )
            resume = ResumeRepository(session).upsert(
                account.id,
                "supervised-preflight-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "supervised-preflight-vacancy",
                    "Python backend",
                    "https://hh.ru/vacancy/supervised-preflight-vacancy",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=91,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 91, now)
            service = ApplicationAutomationService(session)

            job = service.claim_supervised_form_preflight(
                account_id=account.id,
                task_id=task.id,
                now=now,
            )

            assert job.task.id == task.id
            assert job.vacancy.hh_id == "supervised-preflight-vacancy"
            assert job.cover_letter is None
            assert job.task.last_error_code == FORM_PREFLIGHT_RUNNING
            service.release_form_preflight(job, now=now)
            released = QueueTaskRepository(session).get(task.id)
            assert released.state is TaskState.RETRY_SCHEDULED
            assert released.last_error_code == FORM_PREFLIGHT_PASSED

            SystemStateRepository(session).transition(SystemState.RUNNING)
            with pytest.raises(RuntimeError, match="поставьте отправку откликов на паузу"):
                service.claim_supervised_form_preflight(
                    account_id=account.id,
                    task_id=task.id,
                    now=now,
                )
    finally:
        database.close()


def test_automation_prepares_claims_and_records_results(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "account-1")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-1",
                "Python backend разработчик",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancies = VacancyRepository(session)
            match = vacancies.upsert(
                VacancyData("100", "Python developer", "https://hh.ru/vacancy/100")
            )
            stretch = vacancies.upsert(
                VacancyData("200", "AI Agent Engineer", "https://hh.ru/vacancy/200")
            )
            directions.track_vacancy(direction.id, match.id)
            directions.track_vacancy(direction.id, stretch.id)
            directions.apply_rules(
                direction.id,
                match.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            directions.apply_rules(
                direction.id,
                stretch.id,
                state=VacancyState.ANALYZED,
                score=60,
                details={"category": "STRETCH", "accepted": True},
                rules_version=RULES_VERSION,
            )

            service = ApplicationAutomationService(session)
            prepared = service.prepare(
                account_external_id="account-1",
                direction_name="Python backend",
                include_stretch=True,
            )
            assert prepared.created == 2
            assert prepared.resume == resume
            repeated = service.prepare_for_account_id(
                account_id=account.id,
                direction_name="Python backend",
                include_stretch=True,
            )
            assert repeated.created == 0
            assert repeated.existing == 2
            assert service.recover_interrupted() == 0
            SystemStateRepository(session).transition(SystemState.RUNNING)

            first = service.claim_next(direction.id)
            assert first is not None
            assert first.vacancy.hh_id == "100"
            needs_input = service.record_result(
                first,
                HhApplyResult(
                    HhApplyStatus.QUESTIONS_REQUIRED,
                    first.vacancy.source_url,
                    questions=("Личный вопрос",),
                ),
            )
            assert not needs_input.sent
            assert (
                ApplicationRepository(session).get(first.application.id).state
                is ApplicationState.APPLYING
            )
            assert QueueTaskRepository(session).get(first.task.id).state is TaskState.INPUT_REQUIRED

            assert service.claim_next(direction.id, include_stretch=False) is None
            second = service.claim_next(direction.id)
            assert second is not None
            assert second.vacancy.hh_id == "200"
            HhSynchronizationService(session).synchronize_statuses(
                account_id=account.id,
                statuses=(
                    HhNegotiationData(
                        second.vacancy.hh_id,
                        HhNegotiationStatus.APPLIED,
                        "Не просмотрен",  # noqa: RUF001
                        True,
                    ),
                ),
            )
            assert QueueTaskRepository(session).get(second.task.id).state is TaskState.COMPLETED
            directions.apply_rules(
                direction.id,
                second.vacancy.id,
                state=VacancyState.QUEUED,
                score=15,
                details={
                    "category": "MATCH",
                    "accepted": True,
                    "reasons": ["оценка изменилась после получения задания"],
                },
                rules_version="snapshot-rules-after-claim",
            )
            recorded = service.record_result(
                second,
                HhApplyResult(HhApplyStatus.APPLIED, second.vacancy.source_url, "успешно"),
                apply_delay=timedelta(seconds=45),
            )
            assert recorded.sent
            assert recorded.next_apply_at is not None
            assert (
                ApplicationRepository(session).get(second.application.id).state
                is ApplicationState.APPLIED
            )
            assert QueueTaskRepository(session).get(second.task.id).state is TaskState.COMPLETED
            assert service.applied_since(account.id, datetime(2026, 1, 1, tzinfo=UTC)) == 1
            second_applied_events = [
                event
                for event in ApplicationRepository(session).list_events(second.application.id)
                if event.event_type is ApplicationEventType.APPLIED
            ]
            assert len(second_applied_events) == 2
            assert [event.payload["source"] for event in second_applied_events] == [
                "hh.ru",
                "hugin_send",
            ]
            assert all(
                event.payload["category"] == "STRETCH"
                and event.payload["fit_score"] == 60
                and event.payload["rules_version"] == RULES_VERSION
                and event.payload["direction_id"] == direction.id
                and event.payload["resume_id"] == resume.id
                for event in second_applied_events
            )
            SystemStateRepository(session).set_next_apply_at(None)

            already_vacancy = vacancies.upsert(
                VacancyData("already", "Python", "https://hh.ru/vacancy/already")
            )
            directions.track_vacancy(direction.id, already_vacancy.id)
            directions.apply_rules(
                direction.id,
                already_vacancy.id,
                state=VacancyState.QUEUED,
                score=55,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            already_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                already_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(already_application.id, 55)
            already_job = service.claim_next(direction.id)
            assert already_job is not None
            already = service.record_result(
                already_job,
                HhApplyResult(HhApplyStatus.ALREADY_APPLIED, already_vacancy.source_url),
            )
            assert not already.sent
            already_event = ApplicationRepository(session).list_events(already_application.id)[-1]
            assert already_event.payload["source"] == "hh.ru"
            assert "selection_snapshot" not in already_event.payload
            assert "snapshot_missing" not in already_event.payload
            assert service.applied_since(account.id, datetime(2026, 1, 1, tzinfo=UTC)) == 1

            uncertain_vacancy = vacancies.upsert(
                VacancyData("300", "Python engineer", "https://hh.ru/vacancy/300")
            )
            directions.track_vacancy(direction.id, uncertain_vacancy.id)
            directions.apply_rules(
                direction.id,
                uncertain_vacancy.id,
                state=VacancyState.QUEUED,
                score=50,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            uncertain_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                uncertain_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(uncertain_application.id, 50)
            uncertain_job = service.claim_next(direction.id)
            assert uncertain_job is not None
            uncertain = service.record_result(
                uncertain_job,
                HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, uncertain_vacancy.source_url),
            )
            assert not uncertain.blocking
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state
                is TaskState.UNKNOWN_RESULT
            )
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
            unknown_event = ApplicationRepository(session).list_events(
                uncertain_job.application.id
            )[-1]
            assert unknown_event.payload["final_url"] == uncertain_vacancy.source_url
            attempt_snapshot = unknown_event.payload["selection_snapshot"]
            assert isinstance(attempt_snapshot, dict)
            assert attempt_snapshot["category"] == "MATCH"
            assert attempt_snapshot["fit_score"] == 50
            assert attempt_snapshot["rules_version"] == RULES_VERSION
            assert attempt_snapshot["rules_details"] == {
                "category": "MATCH",
                "accepted": True,
            }
            assert attempt_snapshot["direction_id"] == direction.id
            assert attempt_snapshot["resume_id"] == resume.id
            assert attempt_snapshot["cover_letter_id"] is None
            assert (
                service.applied_since(
                    account.id,
                    datetime(2026, 1, 1, tzinfo=UTC),
                )
                == 2
            )

            directions.apply_rules(
                direction.id,
                uncertain_vacancy.id,
                state=VacancyState.QUEUED,
                score=25,
                details={
                    "category": "STRETCH",
                    "accepted": True,
                    "reasons": ["правила изменились после попытки"],
                },
                rules_version="snapshot-rules-v2",
            )

            confirmed = ApplicationReconciliationService(session).reconcile(
                uncertain_job.task.id,
                ApplicationReconciliationResult(
                    ReconciliationStatus.APPLIED,
                    final_url="https://hh.ru/applicant/negotiations",
                    confirmation="Найдено в списке откликов",
                ),
            )
            assert not confirmed.blocking
            assert (
                ApplicationRepository(session).get(uncertain_job.application.id).state
                is ApplicationState.APPLIED
            )
            assert (
                QueueTaskRepository(session).get(uncertain_job.task.id).state is TaskState.COMPLETED
            )
            reconciled_event = ApplicationRepository(session).list_events(
                uncertain_job.application.id
            )[-1]
            assert reconciled_event.event_type is ApplicationEventType.APPLIED
            assert reconciled_event.payload["category"] == "MATCH"
            assert reconciled_event.payload["fit_score"] == 50
            assert reconciled_event.payload["rules_version"] == RULES_VERSION
            assert reconciled_event.payload["rules_details"] == {
                "category": "MATCH",
                "accepted": True,
            }
            assert reconciled_event.payload["direction_id"] == direction.id
            assert reconciled_event.payload["resume_id"] == resume.id
            assert reconciled_event.payload["task_id"] == uncertain_job.task.id
            assert reconciled_event.payload["snapshot_missing"] is False
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
            assert (
                service.applied_since(
                    account.id,
                    datetime(2026, 1, 1, tzinfo=UTC),
                )
                == 2
            )

            closed_vacancy = vacancies.upsert(
                VacancyData("400", "Closed Python role", "https://hh.ru/vacancy/400")
            )
            directions.track_vacancy(direction.id, closed_vacancy.id)
            directions.apply_rules(
                direction.id,
                closed_vacancy.id,
                state=VacancyState.QUEUED,
                score=40,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            closed_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                closed_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(closed_application.id, 40)
            closed_job = service.claim_next(direction.id)
            assert closed_job is not None
            closed = service.record_result(
                closed_job,
                HhApplyResult(HhApplyStatus.VACANCY_CLOSED, closed_vacancy.source_url),
            )
            assert not closed.sent
            assert (
                ApplicationRepository(session).get(closed_application.id).state
                is ApplicationState.CLOSED
            )
            assert QueueTaskRepository(session).get(closed_job.task.id).state is TaskState.SKIPPED
            assert (
                VacancyRepository(session).get(closed_vacancy.id).availability
                is VacancyAvailability.CLOSED
            )
            assert (
                DirectionRepository(session)
                .get_tracked_vacancy(direction.id, closed_vacancy.id)
                .state
                is VacancyState.CLOSED
            )

            auth_vacancy = vacancies.upsert(
                VacancyData("500", "Protected Python role", "https://hh.ru/vacancy/500")
            )
            directions.track_vacancy(direction.id, auth_vacancy.id)
            directions.apply_rules(
                direction.id,
                auth_vacancy.id,
                state=VacancyState.QUEUED,
                score=30,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            auth_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                auth_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(auth_application.id, 30)
            auth_job = service.claim_next(direction.id)
            assert auth_job is not None
            auth_required = service.record_result(
                auth_job,
                HhApplyResult(HhApplyStatus.AUTH_REQUIRED, auth_vacancy.source_url),
            )
            assert auth_required.blocking
            assert SystemStateRepository(session).get().state is SystemState.AUTH_REQUIRED
            service.resume_after_authentication()
            assert SystemStateRepository(session).get().state is SystemState.RUNNING
    finally:
        database.close()


def test_retry_after_schedules_next_run_and_account_warning_blocks_queue(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC) - timedelta(seconds=1)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "retry-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            direction = DirectionRepository(session).create(account.id, "ИТ")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("retry", "Python", "https://hh.ru/vacancy/retry")
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=50,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(application.id, 50, now)
            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)
            job = service.claim_next(direction.id)
            assert job is not None

            recorded = service.record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.RETRYABLE_ERROR,
                    vacancy.source_url,
                    retry_after_seconds=120,
                    retry_blocks_queue=True,
                ),
                now=now,
            )

            expected = now + timedelta(seconds=120)
            assert recorded.next_apply_at == expected
            assert QueueTaskRepository(session).get(job.task.id).scheduled_at == expected
            assert SystemStateRepository(session).get().next_apply_at == expected

            SystemStateRepository(session).set_next_apply_at(None)
            warning_vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "account-warning",
                    "Python",
                    "https://hh.ru/vacancy/account-warning",
                )
            )
            directions.track_vacancy(direction.id, warning_vacancy.id)
            directions.apply_rules(
                direction.id,
                warning_vacancy.id,
                state=VacancyState.QUEUED,
                score=40,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            warning_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                warning_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(warning_application.id, 40, now)
            warning_job = service.claim_next(direction.id)
            assert warning_job is not None
            warning = service.record_result(
                warning_job,
                HhApplyResult(
                    HhApplyStatus.ACCOUNT_WARNING,
                    warning_vacancy.source_url,
                ),
                now=now,
            )

            assert warning.blocking
            assert SystemStateRepository(session).get().state is SystemState.ACCOUNT_WARNING
    finally:
        database.close()


def test_repeated_failure_stops_after_second_attempt(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC) - timedelta(seconds=1)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "retry-limit-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-retry-limit", "Python")
            direction = DirectionRepository(session).create(account.id, "Python backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "retry-limit",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/retry-limit",
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 80, now)
            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)

            for _attempt in range(2):
                job = service.claim_next(direction.id)
                assert job is not None
                service.record_result(
                    job,
                    HhApplyResult(
                        HhApplyStatus.RETRYABLE_ERROR,
                        vacancy.source_url,
                        "Форма отклика не открылась",
                    ),
                    retry_delay=timedelta(0),
                    now=now,
                )

            stored = QueueTaskRepository(session).get(task.id)
            assert stored.state is TaskState.REVIEW_REQUIRED
            assert stored.last_error_code == "RETRY_LIMIT_REACHED"
            incident = session.scalar(
                select(IncidentModel).where(
                    IncidentModel.code == "APPLICATION_RETRY_EXHAUSTED",
                    IncidentModel.scope_id == task.id,
                )
            )
            assert incident is not None
            assert incident.state is IncidentState.OPEN
    finally:
        database.close()


def test_temporary_network_failure_remains_scheduled_after_second_attempt(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC) - timedelta(seconds=121)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "network-retry-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-network-retry",
                "Python",
            )
            direction = DirectionRepository(session).create(account.id, "Python backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "network-retry",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/network-retry",
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 80, now)
            following_vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "following-network-retry",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/following-network-retry",
                )
            )
            directions.track_vacancy(direction.id, following_vacancy.id)
            directions.apply_rules(
                direction.id,
                following_vacancy.id,
                state=VacancyState.QUEUED,
                score=70,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            following_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                following_vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(following_application.id, 70, now)
            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)

            failed_at = now
            for _attempt in range(2):
                job = service.claim_next(direction.id)
                assert job is not None
                service.record_result(
                    job,
                    HhApplyResult(
                        HhApplyStatus.RETRYABLE_ERROR,
                        vacancy.source_url,
                        "Vacancy page did not load",
                        retry_after_seconds=60,
                    ),
                    now=failed_at,
                )
                failed_at += timedelta(seconds=60)

            stored = QueueTaskRepository(session).get(task.id)
            assert stored.state is TaskState.RETRY_SCHEDULED
            assert stored.last_error_code == HhApplyStatus.RETRYABLE_ERROR.value
            assert stored.scheduled_at == failed_at
            assert SystemStateRepository(session).get().next_apply_at is None
            following_job = service.claim_next(
                direction.id,
                now=failed_at - timedelta(seconds=60),
            )
            assert following_job is not None
            assert following_job.vacancy.hh_id == following_vacancy.hh_id
            incident = session.scalar(
                select(IncidentModel).where(
                    IncidentModel.code == "APPLICATION_RETRY_EXHAUSTED",
                    IncidentModel.scope_id == task.id,
                )
            )
            assert incident is None
    finally:
        database.close()


@pytest.mark.parametrize("retry_blocks_queue", [False, True])
def test_temporary_network_failure_stops_after_five_attempts(
    settings: Settings,
    *,
    retry_blocks_queue: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    failed_at = datetime.now(UTC) - timedelta(minutes=10)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "bounded-network-retry")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-bounded-network-retry",
                "Python",
            )
            direction = DirectionRepository(session).create(account.id, "Python backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "bounded-network-retry",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/bounded-network-retry",
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 80, failed_at)
            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)

            recorded = None
            for _attempt in range(application_automation_module.MAX_SCHEDULED_RETRY_ATTEMPTS):
                job = service.claim_next(direction.id, now=failed_at)
                assert job is not None
                recorded = service.record_result(
                    job,
                    HhApplyResult(
                        HhApplyStatus.RETRYABLE_ERROR,
                        vacancy.source_url,
                        "Vacancy page did not load",
                        retry_after_seconds=60,
                        retry_blocks_queue=retry_blocks_queue,
                    ),
                    now=failed_at,
                )
                failed_at += timedelta(seconds=60)

            stored = QueueTaskRepository(session).get(task.id)
            assert stored.state is TaskState.REVIEW_REQUIRED
            assert stored.last_error_code == "RETRY_LIMIT_REACHED"
            expected_next_apply_at = failed_at if retry_blocks_queue else None
            assert recorded is not None
            assert recorded.next_apply_at == expected_next_apply_at
            assert SystemStateRepository(session).get().next_apply_at == expected_next_apply_at
            incident = session.scalar(
                select(IncidentModel).where(
                    IncidentModel.code == "APPLICATION_RETRY_EXHAUSTED",
                    IncidentModel.scope_id == task.id,
                )
            )
            assert incident is not None
            assert incident.state is IncidentState.OPEN
    finally:
        database.close()


def test_priority_guard_sees_scheduled_application_work(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "scheduled-guard-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "resume-scheduled-guard",
                "Python",
            )
            direction = DirectionRepository(session).create(account.id, "Python backend")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "scheduled-guard",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/scheduled-guard",
                )
            )
            directions = DirectionRepository(session)
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            QueueTaskRepository(session).enqueue(
                application.id,
                80,
                now + timedelta(minutes=10),
            )
            SystemStateRepository(session).transition(SystemState.RUNNING)
            service = ApplicationAutomationService(session)

            assert not service.has_pending_application_work(
                account_id=account.id,
                now=now,
            )
            assert service.has_pending_application_work(
                account_id=account.id,
                include_scheduled=True,
                now=now,
            )
    finally:
        database.close()


def test_rule_change_skips_and_can_restore_pending_task(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "rules-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-rules", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData("rules", "Python backend", "https://hh.ru/vacancy/rules")
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            service = ApplicationAutomationService(session)
            assert (
                service.prepare_for_account_id(
                    account_id=account.id,
                    direction_name=direction.name,
                    include_stretch=True,
                ).created
                == 1
            )
            application = ApplicationRepository(session).get_by_key(
                account.id,
                vacancy.id,
                resume.id,
            )
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None

            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.FILTERED_OUT,
                score=0,
                details={"category": "REJECTED", "accepted": False},
                rules_version=RULES_VERSION,
            )
            service.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=True,
            )
            skipped = QueueTaskRepository(session).get(task.id)
            assert skipped.state is TaskState.SKIPPED
            assert skipped.last_error_code == "VACANCY_RULES_CHANGED"

            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=75,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            restored = service.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=True,
            )

            assert restored.created == 1
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING
    finally:
        database.close()


@pytest.mark.parametrize(
    "task_state",
    (TaskState.REVIEW_REQUIRED, TaskState.INPUT_REQUIRED),
)
def test_rule_change_skips_actionable_task_waiting_for_user(
    settings: Settings,
    task_state: TaskState,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create(
                "Иван",
                f"rules-waiting-{task_state.value}",
            )
            resume = ResumeRepository(session).upsert(
                account.id,
                f"resume-{task_state.value}",
                "Python",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    f"rules-{task_state.value}",
                    "Python backend",
                    f"https://hh.ru/vacancy/rules-{task_state.value}",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.FILTERED_OUT,
                score=0,
                details={"category": "REJECTED", "accepted": False},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 50)
            if task_state is TaskState.INPUT_REQUIRED:
                tasks.transition(task.id, TaskState.RUNNING)
            tasks.transition(task.id, task_state)

            skipped = tasks.skip_ineligible(
                direction.id,
                rules_version=RULES_VERSION,
                allowed_categories=frozenset(
                    {RuleCategory.MATCH.value, RuleCategory.STRETCH.value}
                ),
            )

            assert skipped == 1
            stored = tasks.get(task.id)
            assert stored.state is TaskState.SKIPPED
            assert stored.last_error_code == "VACANCY_RULES_CHANGED"
    finally:
        database.close()


def test_routed_pending_application_moves_to_target_direction(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "routed-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "routed-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            source = directions.create(account.id, "Python backend")
            target = directions.create(account.id, "ИТ")
            directions.attach_resume(source.id, resume.id)
            directions.attach_resume(target.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "routed-vacancy",
                    "Junior Data-инженер",
                    "https://hh.ru/vacancy/routed-vacancy",
                )
            )
            directions.track_vacancy(source.id, vacancy.id)
            directions.track_vacancy(target.id, vacancy.id)
            directions.apply_rules(
                source.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            service = ApplicationAutomationService(session)
            assert (
                service.prepare_for_account_id(
                    account_id=account.id,
                    direction_name=source.name,
                    include_stretch=False,
                ).created
                == 1
            )
            application = ApplicationRepository(session).get_by_key(
                account.id,
                vacancy.id,
                resume.id,
            )
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None

            directions.apply_rules(
                source.id,
                vacancy.id,
                state=VacancyState.SKIPPED,
                score=0,
                details={
                    "category": "ROUTED",
                    "accepted": False,
                    "target_scope": "IT_ADJACENT",
                },
                rules_version=RULES_VERSION,
            )
            directions.apply_rules(
                target.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=75,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            service.prepare_for_account_id(
                account_id=account.id,
                direction_name=source.name,
                include_stretch=False,
            )

            created = service.prepare_vacancies(
                account_external_id=account.external_id or "",
                vacancy_ids=(vacancy.id,),
                include_stretch=False,
            )

            assert created == 1
            moved = ApplicationRepository(session).get(application.id)
            assert moved.direction_id == target.id
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING
    finally:
        database.close()


def test_prepare_recovers_letter_task_stopped_for_missing_evidence(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "letter-recovery-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "letter-recovery-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "letter-recovery-vacancy",
                    "Python backend разработчик",
                    "https://hh.ru/vacancy/letter-recovery-vacancy",
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            service = ApplicationAutomationService(session)
            assert (
                service.prepare_for_account_id(
                    account_id=account.id,
                    direction_name=direction.name,
                    include_stretch=False,
                ).created
                == 1
            )
            application = ApplicationRepository(session).get_by_key(
                account.id,
                vacancy.id,
                resume.id,
            )
            assert application is not None
            tasks = QueueTaskRepository(session)
            task = tasks.get_by_application_id(application.id)
            assert task is not None
            tasks.transition(
                task.id,
                TaskState.SKIPPED,
                error_code="NO_RELEVANT_EVIDENCE",
            )

            restored = service.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=False,
            )

            assert restored.created == 1
            recovered = tasks.get(task.id)
            assert recovered.state is TaskState.PENDING
            assert recovered.last_error_code is None
    finally:
        database.close()


def test_prepare_promotes_sendable_duplicate_when_family_has_no_application(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "duplicate-recovery-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "duplicate-recovery-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancies = VacancyRepository(session)
            canonical = vacancies.upsert(
                VacancyData(
                    "duplicate-old",
                    "Python backend разработчик",
                    "https://hh.ru/vacancy/duplicate-old",
                    employer_name="Компания",
                    description="Разработка сервисов на Python и FastAPI.",
                    details_fetched_at=now,
                )
            )
            current = vacancies.upsert(
                VacancyData(
                    "duplicate-current",
                    "Python backend разработчик",
                    "https://hh.ru/vacancy/duplicate-current",
                    employer_name="Компания",
                    description="Разработка сервисов на Python и FastAPI.",
                    details_fetched_at=now,
                )
            )
            vacancies.mark_duplicate(current.id, canonical.id, 0.99)
            directions.track_vacancy(direction.id, current.id)
            directions.apply_rules(
                direction.id,
                current.id,
                state=VacancyState.ANALYZED,
                score=85,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )

            prepared = ApplicationAutomationService(session).prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=False,
            )

            assert prepared.created == 1
            assert vacancies.get(current.id).duplicate_of_id is None
            assert vacancies.get(canonical.id).duplicate_of_id == current.id
            application = ApplicationRepository(session).get_by_key(
                account.id,
                current.id,
                resume.id,
            )
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None
            assert task.state is TaskState.PENDING
    finally:
        database.close()


def test_reanalysis_skips_pending_task_for_vacancy_older_than_thirty_days(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    now = datetime.now(UTC)

    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "old-vacancy-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "old-vacancy-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "old-vacancy",
                    "Python backend разработчик",
                    "https://hh.ru/vacancy/old-vacancy",
                    published_at=now - timedelta(days=31),
                    description="Разработка backend-службы на Python и FastAPI",
                    key_skills=("Python", "FastAPI", "PostgreSQL"),
                    details_fetched_at=now,
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.ANALYZED,
                score=80,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            applications = ApplicationAutomationService(session)
            assert (
                applications.prepare_for_account_id(
                    account_id=account.id,
                    direction_name=direction.name,
                    include_stretch=False,
                ).created
                == 1
            )
            application = ApplicationRepository(session).get_by_key(
                account.id,
                vacancy.id,
                resume.id,
            )
            assert application is not None
            task = QueueTaskRepository(session).get_by_application_id(application.id)
            assert task is not None
            assert task.state is TaskState.PENDING

            analyzed = VacancyAnalysisService(session).reanalyze(
                account_external_id=account.external_id or "",
                direction_name=direction.name,
            )
            applications.prepare_for_account_id(
                account_id=account.id,
                direction_name=direction.name,
                include_stretch=False,
            )

            assert len(analyzed) == 1
            assert analyzed[0].evaluation.category is RuleCategory.REJECTED
            assert analyzed[0].state is VacancyState.FILTERED_OUT
            assert analyzed[0].vacancy.availability is VacancyAvailability.ACTIVE
            skipped = QueueTaskRepository(session).get(task.id)
            assert skipped.state is TaskState.SKIPPED
            assert skipped.last_error_code == "VACANCY_RULES_CHANGED"
    finally:
        database.close()


def test_review_claim_is_allowed_only_while_queue_is_paused(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)

            assert service.claim_next(allow_paused_review=True) is None

            service.acquire_supervised_lease("review-lease")
            with pytest.raises(RuntimeError, match="во время управляемого отклика"):
                service.claim_next(allow_paused_review=True)
            service.release_supervised_lease("review-lease")

            SystemStateRepository(session).transition(SystemState.RUNNING)
            with pytest.raises(RuntimeError, match="только на паузе"):
                service.claim_next(allow_paused_review=True)
    finally:
        database.close()


def test_background_claim_and_submit_guard_require_the_same_current_letter(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "background-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "background-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "background-vacancy",
                    "Python backend",
                    "https://hh.ru/vacancy/background-vacancy",
                    description=(
                        "Разработка серверных приложений на Python, прикладной логики, "
                        "обработки ошибок и контроля целостности данных."
                    ),
                    key_skills=("Python",),
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=90,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 90)
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Иван",
            )
            session.add(profile)
            session.flush()
            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="work_experience",
                    content=(
                        "Разрабатываю серверные приложения на Python, реализую прикладную "
                        "логику, обрабатываю ошибки, разделяю прикладную часть и доступ к "
                        "данным, использую автоматические проверки и слежу за целостностью "
                        "данных."
                    ),
                    source_type="test",
                    resume_id=resume.id,
                    direction_id=direction.id,
                    state=ConfirmationState.CONFIRMED,
                    allow_in_letters=True,
                )
            )
            session.flush()
            text = _supervised_letter()
            letter = CoverLetterModel(
                application_id=application.id,
                vacancy_id=vacancy.id,
                direction_id=direction.id,
                resume_id=resume.id,
                text=text,
                instruction_version=cover_letter_instruction_version(
                    DEFAULT_AI_PROMPTS.cover_letter
                ),
                model_name="old-model",
                state=CoverLetterState.READY,
            )
            session.add(letter)
            session.flush()
            fact_id = session.scalar(select(VerifiedFactModel.id).limit(1))
            assert fact_id is not None
            session.add(CoverLetterFactModel(cover_letter_id=letter.id, fact_id=fact_id))
            letter.reused_from_id = letter.id
            letter.context_hash = CoverLetterService(session).current_context_hash(application.id)
            session.flush()

            service = ApplicationAutomationService(session)
            SystemStateRepository(session).transition(SystemState.RUNNING)
            assert service.claim_next(require_cover_letter=True) is None
            stale_task = QueueTaskRepository(session).get(task.id)
            assert stale_task.state is TaskState.RETRY_SCHEDULED
            assert stale_task.last_error_code == "COVER_LETTER_STALE"
            assert letter.state is CoverLetterState.FAILED
            assert letter.text is None
            assert letter.failure_reason == "COVER_LETTER_STALE"
            assert letter.reused_from_id is None
            assert (
                session.scalar(
                    select(CoverLetterFactModel.fact_id).where(
                        CoverLetterFactModel.cover_letter_id == letter.id
                    )
                )
                is None
            )
            assert ApplicationAutomationService(session).recover_interrupted() == 0
            preflight = service.claim_next_form_preflight(account_id=account.id)
            assert preflight is not None
            assert preflight.application.id == application.id
            service.release_form_preflight(preflight)

            letter.model_name = MANUAL_REVIEW_MODEL
            letter.state = CoverLetterState.READY
            letter.text = text
            letter.failure_reason = None
            letter.quality_score = 10
            letter.quality_passed = True
            letter.quality_version = "cover_letter_quality_v1"
            letter.context_hash = CoverLetterService(session).current_context_hash(application.id)
            session.flush()
            job = service.claim_exact_prepared(
                account_id=account.id,
                task_id=task.id,
            )
            assert job is not None
            assert job.cover_letter_id == letter.id
            assert job.cover_letter_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
            assert service.background_submission_is_allowed(
                task.id,
                letter_id=letter.id,
                letter_sha256=job.cover_letter_sha256,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )

            letter.context_hash = "0" * 64
            session.flush()
            assert not service.background_submission_is_allowed(
                task.id,
                letter_id=letter.id,
                letter_sha256=job.cover_letter_sha256,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )

            tracking = session.get(
                DirectionVacancyModel,
                (direction.id, vacancy.id),
            )
            assert tracking is not None
            tracking.rules_details = {"category": "STRETCH", "accepted": True}
            letter.context_hash = CoverLetterService(session).current_context_hash(application.id)
            session.flush()
            assert service.background_submission_is_allowed(
                task.id,
                letter_id=letter.id,
                letter_sha256=job.cover_letter_sha256,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            autonomy = AutonomyPolicyService(session)
            autonomy.update(
                {
                    **autonomy.get().as_payload(),
                    "auto_apply_stretch": False,
                }
            )
            assert not service.background_submission_is_allowed(
                task.id,
                letter_id=letter.id,
                letter_sha256=job.cover_letter_sha256,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )

            tracking.rules_details = {"category": "REJECTED", "accepted": False}
            letter.context_hash = CoverLetterService(session).current_context_hash(application.id)
            session.flush()
            assert not service.background_submission_is_allowed(
                task.id,
                letter_id=letter.id,
                letter_sha256=job.cover_letter_sha256,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )

            tracking.rules_details = {"category": "MATCH", "accepted": True}
            service.release_after_preview(job)
            letter.context_hash = "0" * 64
            session.flush()
            assert service.claim_next(require_cover_letter=True) is None
            manual_task = QueueTaskRepository(session).get(task.id)
            assert manual_task.state is TaskState.REVIEW_REQUIRED
            assert manual_task.last_error_code == "COVER_LETTER_STALE"
            assert letter.state is CoverLetterState.READY
            assert letter.text == text
    finally:
        database.close()


def test_supervised_claim_requires_exact_letter_and_excludes_worker(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "supervised-account")
            resume = ResumeRepository(session).upsert(
                account.id,
                "supervised-resume",
                "Python backend",
            )
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "supervised-vacancy",
                    "Python backend",
                    "https://hh.ru/vacancy/supervised-vacancy",
                    description=(
                        "Разработка серверных приложений на Python, прикладной логики, "
                        "обработки ошибок и контроля целостности данных."
                    ),
                    key_skills=("Python",),
                )
            )
            directions.track_vacancy(direction.id, vacancy.id)
            directions.apply_rules(
                direction.id,
                vacancy.id,
                state=VacancyState.QUEUED,
                score=90,
                details={"category": "MATCH", "accepted": True},
                rules_version=RULES_VERSION,
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
                direction.id,
            )
            task = QueueTaskRepository(session).enqueue(application.id, 90)
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Иван",
            )
            session.add(profile)
            session.flush()
            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="work_experience",
                    content=(
                        "Разрабатываю серверные приложения на Python, реализую прикладную "
                        "логику, обрабатываю ошибки, разделяю прикладную часть и доступ к "
                        "данным, использую автоматические проверки и слежу за целостностью "
                        "данных."
                    ),
                    source_type="test",
                    resume_id=resume.id,
                    direction_id=direction.id,
                    state=ConfirmationState.CONFIRMED,
                    allow_in_letters=True,
                )
            )
            session.flush()
            text = _supervised_letter()
            letter = CoverLetterModel(
                application_id=application.id,
                vacancy_id=vacancy.id,
                direction_id=direction.id,
                resume_id=resume.id,
                text=text,
                instruction_version=cover_letter_instruction_version(
                    DEFAULT_AI_PROMPTS.cover_letter
                ),
                model_name=MANUAL_REVIEW_MODEL,
                quality_score=10,
                quality_passed=True,
                quality_version="cover_letter_quality_v1",
                state=CoverLetterState.READY,
            )
            session.add(letter)
            session.flush()
            letter.context_hash = CoverLetterService(session).current_context_hash(application.id)
            session.flush()

            service = ApplicationAutomationService(session)
            service.acquire_supervised_lease("lease-one")
            with pytest.raises(RuntimeError, match="другой управляемый сеанс"):
                service.acquire_supervised_lease("lease-two")

            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            session.add(
                ApplicationEventModel(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"hh_status": "APPLIED", "source": "hh.ru"},
                )
            )
            session.flush()
            assert service.last_confirmed_application_at(account.id) is None
            session.execute(
                delete(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == application.id
                )
            )
            session.flush()
            session_limit_application_ids = [application.id]
            for index in range(1, 20):
                counted_vacancy = VacancyRepository(session).upsert(
                    VacancyData(
                        f"supervised-counted-{index}",
                        "Python backend",
                        f"https://hh.ru/vacancy/supervised-counted-{index}",
                    )
                )
                counted_application = ApplicationRepository(session).create_apply_intent(
                    account.id,
                    counted_vacancy.id,
                    resume.id,
                )
                session_limit_application_ids.append(counted_application.id)
            session.add_all(
                ApplicationEventModel(
                    application_id=application_id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"hh_status": "APPLIED"},
                )
                for application_id in session_limit_application_ids
            )
            session.flush()
            assert service.last_confirmed_application_at(account.id) is not None
            assert (
                service.last_confirmed_application_at(
                    account.id,
                    before=datetime(2000, 1, 1, tzinfo=UTC),
                )
                is None
            )
            with pytest.raises(RuntimeError, match="Предел управляемого сеанса"):
                service.claim_supervised(
                    lease_token="lease-one",
                    task_id=task.id,
                    letter_id=letter.id,
                    letter_sha256=digest,
                    account_id=account.id,
                    day_start=datetime(2020, 1, 1, tzinfo=UTC),
                    session_limit=20,
                )
            session.execute(
                delete(ApplicationEventModel).where(
                    ApplicationEventModel.application_id.in_(session_limit_application_ids)
                )
            )
            session.flush()
            interval_now = datetime.now(UTC)
            session.add(
                ApplicationEventModel(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"hh_status": "APPLIED"},
                    created_at=interval_now - timedelta(seconds=30),
                )
            )
            session.flush()
            with pytest.raises(RuntimeError, match="Следующая отправка разрешена"):
                service.claim_supervised(
                    lease_token="lease-one",
                    task_id=task.id,
                    letter_id=letter.id,
                    letter_sha256=digest,
                    now=interval_now,
                )
            session.execute(
                delete(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == application.id
                )
            )
            session.flush()
            with pytest.raises(ValueError, match="изменился после утверждения"):
                service.claim_supervised(
                    lease_token="lease-one",
                    vacancy_hh_id=vacancy.hh_id,
                    letter_id=letter.id,
                    letter_sha256="0" * 64,
                )
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING

            letter.model_name = "old-model"
            session.flush()
            with pytest.raises(ValueError, match="устаревшей версией"):
                service.claim_supervised(
                    lease_token="lease-one",
                    task_id=task.id,
                    letter_id=letter.id,
                    letter_sha256=digest,
                )
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING
            letter.model_name = MANUAL_REVIEW_MODEL
            invalid_text = text.replace(
                "Разрабатываю серверные приложения на Python",
                "Разрабатываю серверные приложения на Python уже 5 лет",
            )
            letter.text = invalid_text
            session.flush()
            with pytest.raises(ValueError, match="появилась цифра"):
                service.claim_supervised(
                    lease_token="lease-one",
                    task_id=task.id,
                    letter_id=letter.id,
                    letter_sha256=hashlib.sha256(invalid_text.encode("utf-8")).hexdigest(),
                )
            assert QueueTaskRepository(session).get(task.id).state is TaskState.PENDING
            letter.text = text
            session.flush()

            job = service.claim_supervised(
                lease_token="lease-one",
                task_id=task.id,
                letter_id=letter.id,
                letter_sha256=digest,
            )
            assert job.task.id == task.id
            assert job.cover_letter_id == letter.id
            assert job.cover_letter_sha256 == digest
            assert QueueTaskRepository(session).get(task.id).state is TaskState.RUNNING
            assert service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            letter.model_name = "old-model"
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            letter.model_name = MANUAL_REVIEW_MODEL
            session.flush()
            guard_now = datetime.now(UTC)
            session.add(
                ApplicationEventModel(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLIED,
                    payload={"hh_status": "APPLIED"},
                    created_at=guard_now - timedelta(seconds=30),
                )
            )
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
                now=guard_now,
            )
            session.execute(
                delete(ApplicationEventModel).where(
                    ApplicationEventModel.application_id == application.id
                )
            )
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256="0" * 64,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            original_text = letter.text
            letter.text = " "
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            letter.text = original_text
            session.flush()
            original_context_hash = letter.context_hash
            letter.context_hash = "0" * 64
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            letter.context_hash = original_context_hash
            session.flush()
            tracked_model = session.get(
                DirectionVacancyModel,
                (direction.id, vacancy.id),
            )
            assert tracked_model is not None
            tracked_model.rules_version = "outdated-rules"
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            tracked_model.rules_version = RULES_VERSION
            resume_model = session.get(ResumeModel, resume.id)
            assert resume_model is not None
            resume_model.updated_at = datetime.now(UTC) + timedelta(seconds=1)
            session.flush()
            assert not service.supervised_submission_is_allowed(
                "lease-one",
                task.id,
                letter_id=letter.id,
                letter_sha256=digest,
                resume_hh_id=resume.hh_id,
                resume_title=resume.title,
            )
            with pytest.raises(RuntimeError, match="уже начатого фонового действия"):
                service.acquire_supervised_lease("lease-three")
            service.release_supervised_claim(
                "lease-one",
                task.id,
                error_code="RESUME_PROFILE_MISMATCH",
            )
            returned = QueueTaskRepository(session).get(task.id)
            assert returned.state is TaskState.RETRY_SCHEDULED
            assert returned.last_error_code == "RESUME_PROFILE_MISMATCH"

            service.release_supervised_lease("lease-one")
            SystemStateRepository(session).transition(SystemState.RUNNING)
            assert service.applications_enabled()
            SystemStateRepository(session).transition(SystemState.PAUSED)
            assert QueueTaskRepository(session).claim_exact(task.id) is not None
            QueueTaskRepository(session).transition(
                task.id,
                TaskState.UNKNOWN_RESULT,
                error_code="UNKNOWN_RESULT",
            )
            service.acquire_supervised_lease("lease-after-unknown")
            assert service.supervised_lease_is_valid("lease-after-unknown")
    finally:
        database.close()


def test_worker_is_disabled_while_supervised_lease_is_active(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = ApplicationAutomationService(session)
            service.acquire_supervised_lease("lease-one")
            with pytest.raises(ValueError, match="управляемый"):
                SystemStateRepository(session).transition(SystemState.RUNNING)
            with pytest.raises(ValueError, match="управляемый"):
                QueueService(session).resume()
            assert not service.applications_enabled()
            service.release_supervised_lease("lease-one")
            QueueService(session).resume()
            assert service.applications_enabled()
    finally:
        database.close()
