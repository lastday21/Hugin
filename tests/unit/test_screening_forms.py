from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    AnswerTemplateModel,
    ApplicationModel,
    ApplicationTaskModel,
    CandidateProfileModel,
    CoverLetterModel,
    ScreeningFormModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain import (
    AnswerSource,
    ConfirmationState,
    HhScreeningField,
    HhScreeningForm,
    ScreeningFormState,
    VacancyData,
)
from hugin.domain.applications import ApplicationState
from hugin.domain.content import CoverLetterState
from hugin.domain.tasks import TaskState
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories import (
    AccountRepository,
    ApplicationRepository,
    QueueTaskRepository,
    ResumeRepository,
)
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.autonomy import AutonomyPolicyService
from hugin.services.screening_forms import ScreeningDraftService

pytestmark = pytest.mark.integration


def test_draft_uses_only_confirmed_safe_answers_and_replaces_changed_form(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "forms-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-1",
                    "Python разработчик",
                    "https://hh.ru/vacancy/vacancy-1",
                    employer_name="Компания",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            session.add(
                CoverLetterModel(
                    application_id=application.id,
                    vacancy_id=vacancy.id,
                    resume_id=resume.id,
                    text="Устаревшее письмо",
                    instruction_version="cover_letter_v10_old",
                    model_name="old-model",
                    state=CoverLetterState.READY,
                )
            )
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Иван",
            )
            session.add(profile)
            session.flush()
            telegram = VerifiedFactModel(
                profile_id=profile.id,
                category="telegram",
                content="@ivan",
                source_type="resume",
                state=ConfirmationState.CONFIRMED,
                allow_in_forms=True,
            )
            salary = VerifiedFactModel(
                profile_id=profile.id,
                category="salary_expectation",
                content="120000 рублей на руки",
                source_type="user",
                state=ConfirmationState.CONFIRMED,
                allow_in_forms=True,
            )
            session.add_all((telegram, salary))
            session.flush()
            session.add(
                AnswerTemplateModel(
                    profile_id=profile.id,
                    key="salary_expectation",
                    question_pattern="Какие зарплатные ожидания?",
                    answer_text="120000 рублей на руки",
                    verified_fact_id=salary.id,
                )
            )
            session.flush()

            draft = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:telegram",
                            "Укажите Telegram",
                            "text",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:salary",
                            "Какие зарплатные ожидания?",
                            "text",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:motivation",
                            "Почему хотите работать у нас?",
                            "textarea",
                            is_required=True,
                        ),
                        HhScreeningField(
                            "name:passport",
                            "Укажите серию и номер паспорта",
                            "text",
                            is_required=True,
                        ),
                    )
                ),
            )

            assert draft.state is ScreeningFormState.INPUT_REQUIRED
            assert draft.answers == {
                "name:telegram": "@ivan",
                "name:salary": "120000 рублей на руки",
            }
            assert draft.questions[0].source is AnswerSource.PROFILE
            assert draft.questions[1].source is AnswerSource.BANK
            assert draft.unanswered_count == 2
            assert draft.cover_letter is None
            pending = ScreeningDraftService(session).list_pending(account.id)
            assert len(pending) == 1
            assert pending[0].form_id == draft.form_id
            assert (
                ScreeningDraftService(session).get_pending(account.id, vacancy.hh_id).form_id
                == draft.form_id
            )

            changed = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:telegram",
                            "Укажите Telegram",
                            "text",
                            is_required=True,
                        ),
                    )
                ),
            )

            assert changed.state is ScreeningFormState.CONFIRMED
            assert changed.answers == {"name:telegram": "@ivan"}
            submission = ScreeningDraftService(session).get_auto_submission(application.id)
            assert submission is not None
            assert submission.form_id == changed.form_id
            assert submission.payload.answers == (("name:telegram", "@ivan"),)
            assert ScreeningDraftService(session).auto_submission_allowed(submission)
            assert session.scalar(select(func.count()).select_from(ScreeningFormModel)) == 1
            ScreeningDraftService(session).invalidate(changed.form_id)
            assert ScreeningDraftService(session).list_pending(account.id) == ()
    finally:
        database.close()


def test_option_answer_is_used_only_on_exact_match(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "options-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("vacancy-2", "Python", "https://hh.ru/vacancy/vacancy-2")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            profile = CandidateProfileModel(account_id=account.id, display_name="Иван")
            session.add(profile)
            session.flush()
            session.add(
                AnswerTemplateModel(
                    profile_id=profile.id,
                    key="work_format",
                    question_pattern="Какой формат работы вам подходит?",
                    answer_text="Удалённо",
                )
            )
            session.flush()

            exact = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "Какой формат работы вам подходит?",
                            "radio",
                            is_required=True,
                            options=("Офис", "Удалённо"),
                        ),
                    )
                ),
            )
            assert exact.answers == {"name:format": "Удалённо"}

            normalized_exact = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "  КАКОЙ   ФОРМАТ РАБОТЫ ВАМ ПОДХОДИТ?  ",
                            "radio",
                            is_required=True,
                            options=("Офис", "Удалённо"),
                        ),
                    )
                ),
            )
            assert normalized_exact.answers == {"name:format": "Удалённо"}

            incompatible = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "Какой формат работы вам подходит?",
                            "radio",
                            is_required=True,
                            options=("Офис", "Гибрид"),
                        ),
                    )
                ),
            )
            assert incompatible.answers == {}
            assert incompatible.state is ScreeningFormState.INPUT_REQUIRED

            changed_wording = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "name:format",
                            "В офисе, гибридно или удалённо вам удобнее работать?",
                            "radio",
                            is_required=True,
                            options=("Офис", "Удалённо"),
                        ),
                    )
                ),
            )
            assert changed_wording.answers == {}
            assert changed_wording.state is ScreeningFormState.INPUT_REQUIRED
    finally:
        database.close()


def test_data_accuracy_confirmation_is_not_answered_from_experience_fact(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "accuracy-confirmation")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("vacancy-accuracy", "Python", "https://hh.ru/vacancy/accuracy")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            profile = CandidateProfileModel(account_id=account.id, display_name="Иван")
            session.add(profile)
            session.flush()
            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="experience",
                    content="Нет",
                    source_type="user",
                    state=ConfirmationState.CONFIRMED,
                    allow_in_forms=True,
                )
            )
            session.flush()

            service = ScreeningDraftService(session)
            draft = service.capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "question:accuracy",
                            (
                                "Настоящим подтверждаю, что предоставленные сведения "
                                "являются достоверными, полными и точными."
                            ),
                            "radio",
                            is_required=True,
                            options=("Да", "Нет"),
                        ),
                    )
                ),
            )

            assert draft.state is ScreeningFormState.CONFIRMED
            assert draft.answers == {"question:accuracy": "Да"}
            assert draft.questions[0].source is AnswerSource.PROFILE
            submission = service.get_auto_submission(application.id)
            assert submission is not None
            assert service.auto_submission_allowed(submission)
    finally:
        database.close()


def test_pending_salary_question_is_reconciled_from_confirmed_profile_fact(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "salary-reconcile-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-salary", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-salary",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/vacancy-salary",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 90)
            tasks.transition(task.id, TaskState.RUNNING)
            tasks.transition(task.id, TaskState.INPUT_REQUIRED)
            profile = CandidateProfileModel(
                account_id=account.id,
                active_resume_id=resume.id,
                display_name="Иван",
            )
            session.add(profile)
            session.flush()
            service = ScreeningDraftService(session)
            draft = service.capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "salary",
                            (
                                "Пожалуйста, уточните Ваши зарплатные ожидания из расчета "
                                "стабильной ежемесячной суммы на руки."
                            ),
                            "textarea",
                            is_required=True,
                        ),
                    )
                ),
            )
            assert draft.state is ScreeningFormState.INPUT_REQUIRED

            session.add(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="salary_expectation",
                    content="120 000 рублей на руки",
                    source_type="user",
                    source_reference="profile-question:salary_expectation",
                    actual_at=datetime.now(UTC),
                    state=ConfirmationState.CONFIRMED,
                    allow_in_forms=True,
                )
            )
            session.flush()

            assert service.reconcile_pending_answers(account.id) == 1
            assert service.list_pending(account.id) == ()
            submission = service.get_auto_submission(application.id)
            assert submission is not None
            assert submission.payload.answers == (("salary", "120 000 рублей на руки"),)
            assert tasks.get(task.id).state is TaskState.RETRY_SCHEDULED
    finally:
        database.close()


def test_pending_forms_hide_inactive_vacancies_and_finished_applications(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "stale-form-account")
            resume = ResumeRepository(session).upsert(account.id, "resume-stale", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-stale",
                    "Python-разработчик",
                    "https://hh.ru/vacancy/vacancy-stale",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            tasks = QueueTaskRepository(session)
            task = tasks.enqueue(application.id, 90)
            tasks.transition(task.id, TaskState.RUNNING)
            tasks.transition(task.id, TaskState.INPUT_REQUIRED)
            session.add(CandidateProfileModel(account_id=account.id, display_name="Иван"))
            session.flush()
            service = ScreeningDraftService(session)
            draft = service.capture(
                application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "motivation",
                            "Почему хотите работать у нас?",
                            "textarea",
                            is_required=True,
                        ),
                    )
                ),
            )
            assert service.list_pending(account.id)[0].form_id == draft.form_id
            stored_task = session.get(ApplicationTaskModel, task.id)
            assert stored_task is not None
            stored_task.state = TaskState.SKIPPED
            session.flush()
            assert service.list_pending(account.id) == ()
            stored_task.state = TaskState.INPUT_REQUIRED
            session.flush()
            checks = service.pending_availability_checks(
                account.id,
                checked_before=datetime.now(UTC),
            )
            assert tuple(check.form_id for check in checks) == (draft.form_id,)

            service.record_availability_check(
                account.id,
                draft.form_id,
                VacancyAvailability.ARCHIVED,
                checked_at=datetime.now(UTC),
            )
            assert service.list_pending(account.id) == ()
            with pytest.raises(LookupError):
                service.get_pending(account.id, vacancy.hh_id)

            stored_vacancy = session.get(VacancyModel, vacancy.id)
            assert stored_vacancy is not None
            assert stored_vacancy.availability is VacancyAvailability.ARCHIVED
            stored_application = session.get(ApplicationModel, application.id)
            assert stored_application is not None
            assert stored_application.state is ApplicationState.CLOSED
            assert tasks.get(task.id).state is TaskState.SKIPPED
    finally:
        database.close()


@pytest.mark.parametrize(
    "question",
    (
        "Пройдите тест и укажите Telegram",
        "Нужно пройти тестирование и указать Telegram",
        "Укажите Telegram после теста",
        "Выполните домашнее задание и укажите Telegram",
        "Домашняя работа: укажите Telegram",
    ),
)
def test_explicit_assignment_is_never_a_simple_form(question: str) -> None:
    field = HhScreeningField(
        "name:telegram",
        question,
        "text",
        is_required=True,
    )

    assert ScreeningDraftService._prohibited(field)
    assert not ScreeningDraftService._simple_structure(HhScreeningForm((field,)))


def test_confirmed_form_answer_is_scoped_reused_and_requeues_application(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Иван", "saved-form-answer")
            resume = ResumeRepository(session).upsert(account.id, "resume-1", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("vacancy-3", "Python", "https://hh.ru/vacancy/vacancy-3")
            )
            application = ApplicationRepository(session).create_apply_intent(
                account.id,
                vacancy.id,
                resume.id,
            )
            task_repository = QueueTaskRepository(session)
            task = task_repository.enqueue(application.id, 90)
            task_repository.transition(task.id, TaskState.RUNNING)
            task_repository.transition(task.id, TaskState.INPUT_REQUIRED)
            session.add(
                CandidateProfileModel(
                    account_id=account.id,
                    active_resume_id=resume.id,
                    display_name="Иван",
                )
            )
            session.flush()
            form = HhScreeningForm(
                fields=(
                    HhScreeningField(
                        "name:format",
                        "Какой формат работы вам подходит?",
                        "radio",
                        is_required=True,
                        options=("Офис", "Удалённо"),
                    ),
                )
            )
            service = ScreeningDraftService(session)
            draft = service.capture(application.id, form)
            assert draft.state is ScreeningFormState.INPUT_REQUIRED

            saved = service.save_confirmed_answers(
                account.id,
                draft.form_id,
                {"name:format": "Удалённо"},
            )

            assert saved.state is ScreeningFormState.CONFIRMED
            assert saved.questions[0].source is AnswerSource.USER
            assert task_repository.get(task.id).state is TaskState.RETRY_SCHEDULED
            template = session.scalar(
                select(AnswerTemplateModel).where(
                    AnswerTemplateModel.question_pattern == "Какой формат работы вам подходит?"
                )
            )
            assert template is not None
            assert template.verified_fact_id is not None
            fact = session.get(VerifiedFactModel, template.verified_fact_id)
            assert fact is not None
            assert fact.resume_id == resume.id
            assert fact.category == "work_format"
            assert fact.actual_at is not None
            assert fact.allow_in_forms
            assert fact.state is ConfirmationState.CONFIRMED

            submission = service.get_auto_submission(application.id)
            assert submission is not None
            assert service.auto_submission_allowed(submission)
            fact.actual_at = datetime.now(UTC) - timedelta(days=31)
            session.flush()
            assert service.get_auto_submission(application.id) is None
            assert not service.auto_submission_allowed(submission)
            autonomy = AutonomyPolicyService(session)
            extended_validity = autonomy.get().as_payload()
            extended_validity["mutable_fact_validity_days"] = 45
            previous_revision = autonomy.get().revision
            assert autonomy.update(extended_validity).revision == previous_revision + 1
            assert service.auto_submission_allowed(submission)
            fact.actual_at = datetime.now(UTC)
            session.flush()
            fact.allow_in_forms = False
            session.flush()
            assert not service.auto_submission_allowed(submission)
            fact.allow_in_forms = True
            session.flush()
            assert service.auto_submission_allowed(submission)
            fact.content = "Офис"
            session.flush()
            assert not service.auto_submission_allowed(submission)
            fact.content = "Удалённо"
            session.flush()
            assert service.auto_submission_allowed(submission)
            disabled_policy = autonomy.get().as_payload()
            disabled_policy["auto_submit_simple_forms"] = False
            autonomy.update(disabled_policy)
            assert not service.auto_submission_allowed(submission)
            enabled_policy = autonomy.get().as_payload()
            enabled_policy["auto_submit_simple_forms"] = True
            autonomy.update(enabled_policy)
            assert service.auto_submission_allowed(submission)

            reused = service.capture(application.id, form)
            assert reused.state is ScreeningFormState.CONFIRMED
            assert reused.questions[0].source is AnswerSource.BANK
            service.mark_sent(
                application.id,
                version_hash=reused.version_hash,
            )
            stored = session.get(ScreeningFormModel, reused.form_id)
            assert stored is not None
            assert stored.state is ScreeningFormState.SENT
            assert service.get_auto_submission(application.id) is None

            serious_vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "vacancy-4",
                    "Python",
                    "https://hh.ru/vacancy/vacancy-4",
                )
            )
            serious_application = ApplicationRepository(session).create_apply_intent(
                account.id,
                serious_vacancy.id,
                resume.id,
            )
            serious = service.capture(
                serious_application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "motivation",
                            "Почему хотите работать у нас?",
                            "textarea",
                            is_required=True,
                        ),
                    )
                ),
            )
            serious = service.save_confirmed_answers(
                account.id,
                serious.form_id,
                {"motivation": "Интересны задачи серверной разработки."},
            )
            assert serious.state is ScreeningFormState.REVIEW_REQUIRED
            assert service.get_auto_submission(serious_application.id) is None

            dangerous = service.capture(
                serious_application.id,
                HhScreeningForm(
                    fields=(
                        HhScreeningField(
                            "passport",
                            "Укажите серию и номер паспорта",
                            "text",
                            is_required=True,
                        ),
                    )
                ),
            )
            with pytest.raises(ValueError, match="непосредственно на hh"):
                service.save_confirmed_answers(
                    account.id,
                    dangerous.form_id,
                    {"passport": "0000 000000"},
                )
    finally:
        database.close()
