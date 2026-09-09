# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationTaskModel,
    CandidateProfileModel,
    ScreeningFormModel,
    ScreeningQuestionModel,
    VerifiedFactModel,
)
from hugin.domain import (
    HhApplyResult,
    HhApplyStatus,
    HhScreeningField,
    HhScreeningForm,
    ScreeningFormState,
    VacancyData,
    VacancyState,
)
from hugin.domain.tasks import TaskState
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    DirectionRepository,
    QueueTaskRepository,
    ResumeRepository,
)
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService, ApplyJob
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.screening_forms import ScreeningDraftService
from hugin.services.vacancy_analysis import RULES_VERSION

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change", ("revoked", "expired", "disabled", "repeat"))
def test_repeated_form_with_blocked_submission_is_visible_and_not_requeued(
    settings: Settings,
    change: str,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "repeated-confirmed-form")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            directions = DirectionRepository(session)
            direction = directions.create(account.id, "Python backend")
            directions.attach_resume(direction.id, resume.id)
            vacancy = VacancyRepository(session).upsert(
                VacancyData("repeat-form", "Python", "https://hh.ru/vacancy/repeat-form")
            )
            directions.track_vacancy(direction.id, vacancy.id)
            tracked = directions.apply_rules(
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
            session.add(CandidateProfileModel(account_id=account.id, display_name="Иван"))
            session.flush()
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 90)
            tasks.transition(task.id, TaskState.RUNNING)
            tasks.transition(task.id, TaskState.INPUT_REQUIRED)
            service = ScreeningDraftService(session)
            form = HhScreeningForm(
                (
                    HhScreeningField(
                        "salary",
                        "Укажите зарплатные ожидания на руки",
                        "textarea",
                        is_required=True,
                    ),
                )
            )
            draft = service.capture(application.id, form)
            service.save_confirmed_answers(
                account.id, draft.form_id, {"salary": "120000 рублей на руки"}
            )
            assert service.get_auto_submission(application.id) is not None
            fact = session.scalar(
                select(VerifiedFactModel).where(VerifiedFactModel.profile_id.is_not(None))
            )
            assert fact is not None
            if change == "revoked":
                fact.allow_in_forms = False
            elif change == "expired":
                fact.actual_at = datetime.now(UTC) - timedelta(days=60)
            elif change == "disabled":
                autonomy = AutonomyPolicyService(session)
                payload = autonomy.get().as_payload()
                payload["auto_submit_simple_forms"] = False
                autonomy.update(payload)
            claimed = tasks.claim_exact(task.id, datetime.now(UTC))
            assert claimed is not None
            job = ApplyJob(claimed, application, vacancy, resume, tracked)
            result = ApplicationAutomationService(session).record_result(
                job,
                HhApplyResult(
                    HhApplyStatus.QUESTIONS_REQUIRED,
                    vacancy.source_url,
                    screening_form=form,
                ),
            )
            assert not result.sent
            assert tasks.get(task.id).state is TaskState.REVIEW_REQUIRED
            pending = service.list_pending(account.id)
            assert len(pending) == 1
            assert pending[0].form_id == draft.form_id
            assert pending[0].answers == {"salary": "120000 рублей на руки"}
            assert pending[0].review_reason
            assert service.get_auto_submission(application.id) is None
            assert service.reconcile_pending_answers(account.id) == 0
    finally:
        database.close()


@pytest.mark.parametrize(
    ("obstacle", "expected_state", "reason"),
    (
        ("warning", ScreeningFormState.REVIEW_REQUIRED, "Предупреждение анкеты"),
        ("manual", ScreeningFormState.REVIEW_REQUIRED, "Укажите пояснение на сайте"),
        ("unknown_type", ScreeningFormState.REVIEW_REQUIRED, "Формат поля"),
        ("missing", ScreeningFormState.INPUT_REQUIRED, "обязательные вопросы"),
        ("exhausted", ScreeningFormState.REVIEW_REQUIRED, "повторно запросил"),
        ("disabled", ScreeningFormState.CONFIRMED, "выключена"),
        ("lost_warning", ScreeningFormState.REVIEW_REQUIRED, "не все сведения"),
    ),
)
def test_confirmation_keeps_submission_obstacles_and_explains_them(
    settings: Settings,
    obstacle: str,
    expected_state: ScreeningFormState,
    reason: str,
) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "form-obstacle")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("form-obstacle", "Python", "https://hh.ru/vacancy/form-obstacle")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            session.add(CandidateProfileModel(account_id=account.id, display_name="Иван"))
            session.flush()
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 90)
            tasks.transition(task.id, TaskState.RUNNING)
            tasks.transition(task.id, TaskState.INPUT_REQUIRED)
            service = ScreeningDraftService(session)
            fields: tuple[HhScreeningField, ...] = (
                HhScreeningField(
                    "motivation",
                    "Почему хотите работать у нас?",
                    "unknown" if obstacle == "unknown_type" else "textarea",
                    is_required=True,
                ),
            )
            if obstacle == "missing":
                fields += (
                    HhScreeningField("second", "Второй вопрос", "textarea", is_required=True),
                )
            form = HhScreeningForm(
                fields,
                ("Проверьте вложение",) if obstacle in {"warning", "lost_warning"} else (),
            )
            draft = service.capture(
                application.id,
                form,
                force_review=obstacle == "manual",
                review_reason="Укажите пояснение на сайте",
            )
            stored = session.get(ScreeningFormModel, draft.form_id)
            assert stored is not None
            if obstacle == "lost_warning":
                stored.submission_block_reason = None
            if obstacle == "exhausted":
                stored_task = session.get(ApplicationTaskModel, task.id)
                assert stored_task is not None
                stored_task.state = TaskState.REVIEW_REQUIRED
                stored_task.last_error_code = "FORM_RETRY_EXHAUSTED"
            if obstacle == "disabled":
                autonomy = AutonomyPolicyService(session)
                payload = autonomy.get().as_payload()
                payload["auto_submit_simple_forms"] = False
                autonomy.update(payload)
            saved = service.save_confirmed_answers(
                account.id,
                draft.form_id,
                {"motivation": "Интересны задачи серверной разработки."},
            )
            assert saved.state is expected_state
            assert saved.review_reason and reason in saved.review_reason
            assert service.get_auto_submission(application.id) is None
            assert service.reconcile_pending_answers(account.id) == 0
            assert stored.state is expected_state
            if obstacle != "disabled":
                assert tasks.get(task.id).state in {
                    TaskState.INPUT_REQUIRED,
                    TaskState.REVIEW_REQUIRED,
                }
    finally:
        database.close()


def test_changed_form_keeps_previous_draft_and_rejects_old_confirmation(settings: Settings) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "changed-form")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("changed-form", "Python", "https://hh.ru/vacancy/changed-form")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id, vacancy.id, resume.id
            )
            session.add(CandidateProfileModel(account_id=account.id, display_name="Иван"))
            session.flush()
            service = ScreeningDraftService(session)
            form = HhScreeningForm(
                (HhScreeningField("details", "Расскажите о проекте", "textarea", is_required=True),)
            )
            first = service.capture(application.id, form)
            first = service.save_confirmed_answers(
                account.id, first.form_id, {"details": "Разрабатываю приложение."}
            )
            submission = service.get_auto_submission(application.id)
            assert submission is not None
            changed = service.capture(
                application.id,
                HhScreeningForm(
                    (
                        HhScreeningField(
                            "details",
                            "Укажите опыт с другой технологией",
                            "textarea",
                            is_required=True,
                        ),
                    )
                ),
            )
            assert changed.form_id != first.form_id
            assert changed.state is ScreeningFormState.INPUT_REQUIRED
            assert changed.review_reason and "изменились" in changed.review_reason
            assert not service.auto_submission_allowed(submission)
            old_form = session.get(ScreeningFormModel, first.form_id)
            assert old_form is not None
            assert old_form.state is ScreeningFormState.INVALIDATED
            assert (
                session.scalar(
                    select(ScreeningQuestionModel.question_text).where(
                        ScreeningQuestionModel.form_id == first.form_id,
                    )
                )
                == "Расскажите о проекте"
            )
            with pytest.raises(ValueError, match="недоступна"):
                service.save_confirmed_answers(
                    account.id, first.form_id, {"details": "Другой ответ"}
                )
    finally:
        database.close()
