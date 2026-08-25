from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    AnswerTemplateModel,
    ApplicationModel,
    ApplicationTaskModel,
    CandidateProfileModel,
    CoverLetterModel,
    ResumeModel,
    ScreeningAnswerModel,
    ScreeningFormModel,
    ScreeningQuestionModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.applications import ApplicationState, EventPayload
from hugin.domain.content import (
    AnswerSource,
    ConfirmationState,
    CoverLetterState,
    ScreeningFormState,
    cover_letter_instruction_version,
)
from hugin.domain.hh import (
    HhScreeningField,
    HhScreeningForm,
    HhScreeningSubmission,
    screening_form_hash,
)
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyAvailability
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.tasks import FORM_PREFLIGHT_PASSED, QueueTaskRepository
from hugin.services.ai_prompts import AiPromptSettingsService
from hugin.services.autonomy import AutonomyPolicy, AutonomyPolicyService


@dataclass(frozen=True, slots=True)
class ScreeningDraftQuestion:
    field_key: str
    question: str
    field_type: str
    is_required: bool
    options: tuple[str, ...]
    answer: str | None
    source: AnswerSource | None


@dataclass(frozen=True, slots=True)
class ScreeningDraft:
    form_id: int
    application_id: int
    vacancy_id: str
    vacancy_title: str
    company: str
    source_url: str
    resume_hh_id: str
    resume_title: str
    version_hash: str
    state: ScreeningFormState
    questions: tuple[ScreeningDraftQuestion, ...]
    cover_letter: str | None = None

    @property
    def answers(self) -> dict[str, str]:
        return {
            question.field_key: question.answer
            for question in self.questions
            if question.answer is not None and question.answer.strip()
        }

    @property
    def unanswered_count(self) -> int:
        return sum(question.answer is None for question in self.questions)


@dataclass(frozen=True, slots=True)
class _ResolvedAnswer:
    text: str
    source: AnswerSource
    verified_fact_id: int | None
    confirmed: bool


@dataclass(frozen=True, slots=True)
class StoredScreeningSubmission:
    form_id: int
    application_id: int
    payload: HhScreeningSubmission


@dataclass(frozen=True, slots=True)
class ScreeningAvailabilityCheck:
    form_id: int
    vacancy_id: str
    source_url: str


QUESTION_KEYS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "salary_expectation",
        (re.compile(r"зарплат|оклад|доход|вознагражден", re.IGNORECASE),),
    ),
    (
        "available_from",
        (
            re.compile(r"когда.*(?:выйти|приступить)", re.IGNORECASE),
            re.compile(r"дата выхода", re.IGNORECASE),
        ),
    ),
    ("work_schedule", (re.compile(r"график", re.IGNORECASE),)),
    ("employment", (re.compile(r"занятост", re.IGNORECASE),)),
    ("relocation", (re.compile(r"переезд", re.IGNORECASE),)),
    ("business_trips", (re.compile(r"командиров", re.IGNORECASE),)),
    (
        "work_format",
        (
            re.compile(r"формат.*работ", re.IGNORECASE),
            re.compile(r"удален|удалён|офис|гибрид", re.IGNORECASE),
        ),
    ),
    ("english_level", (re.compile(r"английск", re.IGNORECASE),)),
    ("citizenship", (re.compile(r"гражданств", re.IGNORECASE),)),
    (
        "work_authorization",
        (re.compile(r"разрешен.*работ|разрешён.*работ", re.IGNORECASE),),
    ),
)

FACT_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "full_name",
        (re.compile(r"(?:ваше|укажите).*\bимя\b|\bфио\b", re.IGNORECASE),),
    ),
    ("email", (re.compile(r"электронн.*почт|e-?mail", re.IGNORECASE),)),
    ("phone", (re.compile(r"телефон|номер.*связ", re.IGNORECASE),)),
    ("telegram", (re.compile(r"telegram|телеграм", re.IGNORECASE),)),
    ("github", (re.compile(r"github", re.IGNORECASE),)),
    ("location", (re.compile(r"город.*прожив|место.*жительств", re.IGNORECASE),)),
    ("citizenship", (re.compile(r"гражданств", re.IGNORECASE),)),
    ("employment", (re.compile(r"занятост", re.IGNORECASE),)),
    ("work_format", (re.compile(r"формат.*работ", re.IGNORECASE),)),
    (
        "experience",
        (
            re.compile(r"(?:опыт|стаж).*(?:работ|лет|год|месяц)", re.IGNORECASE),
            re.compile(r"сколько.*(?:лет|год|месяц).*(?:опыт|работ)", re.IGNORECASE),
        ),
    ),
    (
        "technology",
        (
            re.compile(
                r"(?:знаком|владе|работал|работали|опыт).*(?:python|django|fastapi|"
                r"flask|sql|postgres|redis|docker|git|linux|api)",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "portfolio",
        (re.compile(r"портфолио|ссылк.*(?:профил|проект|работ)", re.IGNORECASE),),
    ),
)

DANGEROUS_QUESTION = re.compile(
    r"паспорт|банк|банковск|карт[аы]|снилс|\bинн\b|полис|удостоверен|"
    r"код\s+(?:из|подтверждения)|смс|sms|оплат|перевод.*денег|"
    r"документ|биометр|медицин|здоров|диагноз|судим|"
    r"установ.*программ|испытательн|"
    r"\bтест(?:ы|а|е|ом|у|ами|ах|ов)?\b|\bтестов\w*\b|\bтестирован\w*\b|"
    r"домашн[\s\S]{0,80}(?:задан|работ|проект|тест)|"
    r"(?:прой(?:д|т)|проход|выполн|сдела|реши)[\s\S]{0,120}(?:\bтест\w*|задан)|"
    r"видео",
    re.IGNORECASE,
)

SERIOUS_QUESTION = re.compile(
    r"почему|мотивац|расскаж|опиш|приведите.*пример|задач|решени|"
    r"алгоритм|архитектур|проектир|код|достижен|конфликт|"
    r"сильн.*сторон|слаб.*сторон|причин.*(?:поиск|увольнен)|"
    r"руковод|ожидани.*работодател|эссе|развернут|развёрнут",
    re.IGNORECASE,
)

SERIOUS_OBLIGATION = re.compile(
    r"оформ.*(?:ип|самозан)|штраф|неустой|материальн.*ответствен|"
    r"обязует|обязательств|удержан|платн.*обучен|обучен.*за.*счет",
    re.IGNORECASE,
)

DATA_ACCURACY_CONFIRMATION = re.compile(
    r"(?=.*подтвержд)(?=.*(?:сведен|данн))(?=.*достоверн)",
    re.IGNORECASE | re.DOTALL,
)

SUPPORTED_AUTOMATIC_FIELD_TYPES = frozenset(
    {
        "checkbox",
        "date",
        "email",
        "number",
        "radio",
        "select",
        "tel",
        "text",
        "textarea",
        "url",
    }
)

MUTABLE_FACT_CATEGORIES = frozenset(
    {
        "available_from",
        "business_trips",
        "employment",
        "relocation",
        "salary_expectation",
        "work_format",
        "work_schedule",
    }
)


class ScreeningDraftService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def capture(
        self,
        application_id: int,
        form: HhScreeningForm,
        *,
        force_review: bool = False,
    ) -> ScreeningDraft:
        application = self._session.get(ApplicationModel, application_id)
        if application is None:
            raise LookupError("Отклик не найден")
        policy = AutonomyPolicyService(self._session).get()
        confirmed_at = datetime.now(UTC)
        self._session.execute(
            delete(ScreeningFormModel).where(ScreeningFormModel.application_id == application_id)
        )
        stored = ScreeningFormModel(
            application_id=application_id,
            version_hash=screening_form_hash(form),
            requires_confirmation=(
                force_review
                or not self._simple_structure(form)
                or not policy.auto_submit_simple_forms
            ),
            state=ScreeningFormState.DRAFT,
        )
        self._session.add(stored)
        self._session.flush()

        profile = self._session.scalar(
            select(CandidateProfileModel).where(
                CandidateProfileModel.account_id == application.account_id
            )
        )
        templates = self._templates(profile.id, application) if profile is not None else ()
        facts = self._facts(profile.id, application) if profile is not None else ()
        required_missing = False
        has_unconfirmed_answer = False
        for position, field in enumerate(form.fields):
            question = ScreeningQuestionModel(
                form_id=stored.id,
                field_key=field.key,
                question_text=field.question,
                is_required=field.is_required,
                field_type=field.field_type,
                options=list(field.options),
                max_length=field.max_length,
                format_hint=field.format_hint or None,
                has_attachment=field.has_attachment,
                has_external_action=field.has_external_action,
                has_test_assignment=field.has_test_assignment,
                position=position,
            )
            self._session.add(question)
            self._session.flush()
            resolved = self._resolve(
                field,
                templates,
                facts,
                policy=policy,
                now=confirmed_at,
            )
            if field.is_required and resolved is None:
                required_missing = True
            if resolved is not None and not resolved.confirmed:
                has_unconfirmed_answer = True
            self._session.add(
                ScreeningAnswerModel(
                    question_id=question.id,
                    answer_text=resolved.text if resolved is not None else None,
                    source=resolved.source if resolved is not None else None,
                    verified_fact_id=(resolved.verified_fact_id if resolved is not None else None),
                    is_confirmed=resolved.confirmed if resolved is not None else False,
                    confirmed_at=(
                        confirmed_at if resolved is not None and resolved.confirmed else None
                    ),
                )
            )

        if required_missing:
            stored.state = ScreeningFormState.INPUT_REQUIRED
        elif stored.requires_confirmation or has_unconfirmed_answer:
            stored.state = ScreeningFormState.REVIEW_REQUIRED
        else:
            stored.state = ScreeningFormState.CONFIRMED
            stored.confirmed_at = confirmed_at
        self._session.flush()
        return self._draft(stored)

    def capture_questions(
        self,
        application_id: int,
        questions: tuple[str, ...],
    ) -> ScreeningDraft:
        fields = tuple(
            HhScreeningField(
                key=f"question:{position}:{self._normalize(question)[:220]}",
                question=question,
                field_type="unknown",
                is_required=True,
            )
            for position, question in enumerate(questions)
        )
        return self.capture(application_id, HhScreeningForm(fields))

    def list_pending(self, account_id: int) -> tuple[ScreeningDraft, ...]:
        forms = self._session.scalars(
            select(ScreeningFormModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .outerjoin(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                or_(
                    ApplicationTaskModel.id.is_(None),
                    ApplicationTaskModel.state.not_in((TaskState.SKIPPED, TaskState.COMPLETED)),
                ),
                ScreeningFormModel.state.in_(
                    (
                        ScreeningFormState.REVIEW_REQUIRED,
                        ScreeningFormState.INPUT_REQUIRED,
                    )
                ),
            )
            .order_by(ScreeningFormModel.updated_at, ScreeningFormModel.id)
        )
        return tuple(self._draft(form) for form in forms)

    def get_pending(self, account_id: int, vacancy_id: str) -> ScreeningDraft:
        form = self._session.scalar(
            select(ScreeningFormModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .outerjoin(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.hh_id == vacancy_id,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                or_(
                    ApplicationTaskModel.id.is_(None),
                    ApplicationTaskModel.state.not_in((TaskState.SKIPPED, TaskState.COMPLETED)),
                ),
                ScreeningFormModel.state.in_(
                    (
                        ScreeningFormState.REVIEW_REQUIRED,
                        ScreeningFormState.INPUT_REQUIRED,
                    )
                ),
            )
            .order_by(ScreeningFormModel.updated_at.desc(), ScreeningFormModel.id.desc())
            .limit(1)
        )
        if form is None:
            raise LookupError("Черновик анкеты для этой вакансии не найден")
        return self._draft(form)

    def reconcile_pending_answers(self, account_id: int) -> int:
        forms = tuple(
            self._session.scalars(
                select(ScreeningFormModel)
                .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
                .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
                .outerjoin(
                    ApplicationTaskModel,
                    ApplicationTaskModel.application_id == ApplicationModel.id,
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationModel.state == ApplicationState.APPLYING,
                    VacancyModel.availability == VacancyAvailability.ACTIVE,
                    or_(
                        ApplicationTaskModel.id.is_(None),
                        ApplicationTaskModel.state.not_in((TaskState.SKIPPED, TaskState.COMPLETED)),
                    ),
                    ScreeningFormModel.state.in_(
                        (
                            ScreeningFormState.REVIEW_REQUIRED,
                            ScreeningFormState.INPUT_REQUIRED,
                        )
                    ),
                )
                .order_by(ScreeningFormModel.updated_at, ScreeningFormModel.id)
            )
        )
        reconciled = 0
        policy = AutonomyPolicyService(self._session).get()
        selected_at = datetime.now(UTC)
        for form in forms:
            application = self._session.get(ApplicationModel, form.application_id)
            if application is None:
                continue
            profile = self._session.scalar(
                select(CandidateProfileModel).where(
                    CandidateProfileModel.account_id == application.account_id
                )
            )
            if profile is None:
                continue
            templates = self._templates(profile.id, application)
            facts = self._facts(profile.id, application)
            rows = tuple(
                self._session.execute(
                    select(ScreeningQuestionModel, ScreeningAnswerModel)
                    .outerjoin(
                        ScreeningAnswerModel,
                        ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
                    )
                    .where(ScreeningQuestionModel.form_id == form.id)
                    .order_by(ScreeningQuestionModel.position, ScreeningQuestionModel.id)
                )
            )
            changed = False
            for question, answer in rows:
                if answer is None:
                    answer = ScreeningAnswerModel(question_id=question.id)
                    self._session.add(answer)
                    changed = True
                if answer.is_confirmed and answer.answer_text and answer.answer_text.strip():
                    continue
                resolved = self._resolve(
                    self._stored_field(question),
                    templates,
                    facts,
                    policy=policy,
                    now=selected_at,
                )
                if resolved is None:
                    continue
                if (
                    answer.answer_text != resolved.text
                    or answer.source is not resolved.source
                    or answer.verified_fact_id != resolved.verified_fact_id
                    or answer.is_confirmed != resolved.confirmed
                ):
                    changed = True
                answer.answer_text = resolved.text
                answer.source = resolved.source
                answer.verified_fact_id = resolved.verified_fact_id
                answer.is_confirmed = resolved.confirmed
                answer.confirmed_at = selected_at if resolved.confirmed else None

            if not changed:
                continue
            self._session.flush()
            refreshed_rows = tuple(
                self._session.execute(
                    select(ScreeningQuestionModel, ScreeningAnswerModel)
                    .join(
                        ScreeningAnswerModel,
                        ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
                    )
                    .where(ScreeningQuestionModel.form_id == form.id)
                )
            )
            required_missing = any(
                question.is_required
                and (
                    answer.answer_text is None
                    or not answer.answer_text.strip()
                    or not answer.is_confirmed
                )
                for question, answer in refreshed_rows
            )
            has_unconfirmed_answer = any(
                answer.answer_text is not None
                and answer.answer_text.strip()
                and not answer.is_confirmed
                for _question, answer in refreshed_rows
            )
            if required_missing:
                form.state = ScreeningFormState.INPUT_REQUIRED
                form.confirmed_at = None
            elif form.requires_confirmation or has_unconfirmed_answer:
                form.state = ScreeningFormState.REVIEW_REQUIRED
                form.confirmed_at = None
            else:
                form.state = ScreeningFormState.CONFIRMED
                form.confirmed_at = selected_at
                self._resume_task(form.application_id, selected_at)
            reconciled += 1
        self._session.flush()
        return reconciled

    def pending_availability_checks(
        self,
        account_id: int,
        *,
        checked_before: datetime,
        limit: int = 25,
    ) -> tuple[ScreeningAvailabilityCheck, ...]:
        if limit < 1:
            return ()
        rows = self._session.execute(
            select(ScreeningFormModel, VacancyModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .outerjoin(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.state == ApplicationState.APPLYING,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                or_(
                    ApplicationTaskModel.id.is_(None),
                    ApplicationTaskModel.state.not_in((TaskState.SKIPPED, TaskState.COMPLETED)),
                ),
                ScreeningFormModel.state.in_(
                    (
                        ScreeningFormState.REVIEW_REQUIRED,
                        ScreeningFormState.INPUT_REQUIRED,
                    )
                ),
                (
                    ScreeningFormModel.availability_checked_at.is_(None)
                    | (ScreeningFormModel.availability_checked_at <= as_utc(checked_before))
                ),
            )
            .order_by(
                ScreeningFormModel.availability_checked_at.asc().nullsfirst(),
                ScreeningFormModel.updated_at,
                ScreeningFormModel.id,
            )
            .limit(limit)
        )
        return tuple(
            ScreeningAvailabilityCheck(form.id, vacancy.hh_id, vacancy.source_url)
            for form, vacancy in rows
        )

    def record_availability_check(
        self,
        account_id: int,
        form_id: int,
        availability: VacancyAvailability,
        *,
        checked_at: datetime,
    ) -> None:
        form = self._session.scalar(
            select(ScreeningFormModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .where(
                ScreeningFormModel.id == form_id,
                ApplicationModel.account_id == account_id,
            )
        )
        if form is None:
            return
        form.availability_checked_at = as_utc(checked_at)
        if availability is VacancyAvailability.ACTIVE:
            self._session.flush()
            return
        application = self._session.get(ApplicationModel, form.application_id)
        if application is None:
            return
        vacancy = self._session.get(VacancyModel, application.vacancy_id)
        if vacancy is None:
            return
        vacancy.availability = availability
        form.state = ScreeningFormState.INVALIDATED
        event_payload: EventPayload = {
            "source": "hh.ru",
            "availability": availability.value,
            "form_id": form.id,
        }
        if application.state is ApplicationState.APPLYING:
            ApplicationRepository(self._session).transition_state(
                application.id,
                ApplicationState.CLOSED,
                event_payload,
            )
        task = QueueTaskRepository(self._session).get_by_application_id(application.id)
        if task is not None and task.state in {
            TaskState.PENDING,
            TaskState.RETRY_SCHEDULED,
            TaskState.REVIEW_REQUIRED,
            TaskState.INPUT_REQUIRED,
        }:
            QueueTaskRepository(self._session).transition(
                task.id,
                TaskState.SKIPPED,
                error_code="VACANCY_CLOSED",
                event_payload=event_payload,
            )
        self._session.flush()

    def invalidate(self, form_id: int) -> None:
        form = self._session.get(ScreeningFormModel, form_id)
        if form is None:
            raise LookupError("Черновик анкеты не найден")
        form.state = ScreeningFormState.INVALIDATED
        self._session.flush()

    def save_confirmed_answers(
        self,
        account_id: int,
        form_id: int,
        answers: dict[str, str],
    ) -> ScreeningDraft:
        if not answers:
            raise ValueError("Укажите хотя бы один ответ")
        if len(answers) > 100:
            raise ValueError("За один раз можно сохранить не более 100 ответов")
        form = self._form_for_account(account_id, form_id)
        if form.state in {ScreeningFormState.INVALIDATED, ScreeningFormState.SENT}:
            raise ValueError("Эта версия анкеты уже недоступна для изменения")
        application = self._session.get(ApplicationModel, form.application_id)
        if application is None:
            raise RuntimeError("Отклик анкеты отсутствует")
        rows = tuple(
            self._session.execute(
                select(ScreeningQuestionModel, ScreeningAnswerModel)
                .join(
                    ScreeningAnswerModel,
                    ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
                )
                .where(ScreeningQuestionModel.form_id == form.id)
                .order_by(ScreeningQuestionModel.position, ScreeningQuestionModel.id)
            )
        )
        by_key = {question.field_key: (question, answer) for question, answer in rows}
        unknown = sorted(set(answers) - set(by_key))
        if unknown:
            raise ValueError(f"В текущей анкете нет поля «{unknown[0]}»")

        selected_at = datetime.now(UTC)
        for field_key, raw_value in answers.items():
            question, answer = by_key[field_key]
            field = self._stored_field(question)
            if self._prohibited(field):
                raise ValueError(
                    f"Поле «{question.question_text}» нужно заполнить непосредственно на hh.ru"
                )
            value = raw_value.strip()
            if len(value) > 4000:
                raise ValueError("Ответ слишком длинный")
            compatible = self._compatible_answer(field, value)
            if compatible is None:
                raise ValueError(
                    f"Ответ для поля «{question.question_text}» не соответствует формату"
                )
            fact = self._save_answer_template(
                application,
                question.question_text,
                compatible,
            )
            answer.answer_text = compatible
            answer.source = AnswerSource.USER
            answer.verified_fact_id = fact.id
            answer.is_confirmed = True
            answer.confirmed_at = selected_at

        required_missing = any(
            question.is_required
            and (
                answer.answer_text is None
                or not answer.answer_text.strip()
                or not answer.is_confirmed
            )
            for question, answer in rows
        )
        has_unconfirmed_answer = any(
            answer.answer_text is not None
            and answer.answer_text.strip()
            and not answer.is_confirmed
            for _question, answer in rows
        )
        if required_missing:
            form.state = ScreeningFormState.INPUT_REQUIRED
            form.confirmed_at = None
        elif form.requires_confirmation or has_unconfirmed_answer:
            form.state = ScreeningFormState.REVIEW_REQUIRED
            form.confirmed_at = None
            self._move_task_to_review(form.application_id)
        else:
            form.state = ScreeningFormState.CONFIRMED
            form.confirmed_at = selected_at
            self._resume_task(form.application_id, selected_at)
        self._session.flush()
        return self._draft(form)

    def get_auto_submission(
        self,
        application_id: int,
    ) -> StoredScreeningSubmission | None:
        policy = AutonomyPolicyService(self._session).get()
        if not policy.auto_submit_simple_forms:
            return None
        form = self._session.scalar(
            select(ScreeningFormModel)
            .where(
                ScreeningFormModel.application_id == application_id,
                ScreeningFormModel.state == ScreeningFormState.CONFIRMED,
                ScreeningFormModel.requires_confirmation.is_(False),
            )
            .order_by(ScreeningFormModel.updated_at.desc(), ScreeningFormModel.id.desc())
            .limit(1)
        )
        if form is None:
            return None
        application = self._session.get(ApplicationModel, application_id)
        if application is None or application.state is not ApplicationState.APPLYING:
            return None
        resume = self._session.get(ResumeModel, application.resume_id)
        vacancy = self._session.get(VacancyModel, application.vacancy_id)
        if (
            resume is None
            or not resume.is_active
            or vacancy is None
            or vacancy.availability is not VacancyAvailability.ACTIVE
        ):
            return None
        rows = tuple(
            self._session.execute(
                select(ScreeningQuestionModel, ScreeningAnswerModel)
                .join(
                    ScreeningAnswerModel,
                    ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
                )
                .where(ScreeningQuestionModel.form_id == form.id)
                .order_by(ScreeningQuestionModel.position, ScreeningQuestionModel.id)
            )
        )
        if not rows or any(
            question.is_required
            and (
                answer.answer_text is None
                or not answer.answer_text.strip()
                or not answer.is_confirmed
            )
            for question, answer in rows
        ):
            return None
        if not self._answers_still_allowed(form.id, application, policy):
            return None
        confirmed_answers = tuple(
            (question.field_key, answer.answer_text.strip())
            for question, answer in rows
            if answer.answer_text is not None and answer.answer_text.strip() and answer.is_confirmed
        )
        return StoredScreeningSubmission(
            form_id=form.id,
            application_id=application_id,
            payload=HhScreeningSubmission(form.version_hash, confirmed_answers),
        )

    def auto_submission_allowed(
        self,
        submission: StoredScreeningSubmission,
    ) -> bool:
        policy = AutonomyPolicyService(self._session).get()
        if not policy.auto_submit_simple_forms:
            return False
        current = self.get_auto_submission(submission.application_id)
        if current is None:
            return False
        if current.form_id != submission.form_id or current.payload != submission.payload:
            return False
        application = self._session.get(ApplicationModel, submission.application_id)
        if application is None:
            return False
        return self._answers_still_allowed(current.form_id, application, policy)

    def _answers_still_allowed(
        self,
        form_id: int,
        application: ApplicationModel,
        policy: AutonomyPolicy,
    ) -> bool:
        selected_at = datetime.now(UTC)
        rows = tuple(
            self._session.execute(
                select(
                    ScreeningAnswerModel,
                    VerifiedFactModel,
                    ScreeningQuestionModel,
                )
                .join(
                    ScreeningQuestionModel,
                    ScreeningQuestionModel.id == ScreeningAnswerModel.question_id,
                )
                .outerjoin(
                    VerifiedFactModel,
                    VerifiedFactModel.id == ScreeningAnswerModel.verified_fact_id,
                )
                .where(
                    ScreeningQuestionModel.form_id == form_id,
                    ScreeningAnswerModel.is_confirmed.is_(True),
                    ScreeningAnswerModel.answer_text.is_not(None),
                )
            )
        )
        return all(
            self._answer_still_allowed(
                answer,
                fact,
                question,
                application,
                policy,
                now=selected_at,
            )
            for answer, fact, question in rows
        )

    @classmethod
    def _answer_still_allowed(
        cls,
        answer: ScreeningAnswerModel,
        fact: VerifiedFactModel | None,
        question: ScreeningQuestionModel,
        application: ApplicationModel,
        policy: AutonomyPolicy,
        *,
        now: datetime,
    ) -> bool:
        if fact is None:
            return bool(
                answer.source is AnswerSource.PROFILE
                and answer.answer_text is not None
                and cls._normalize(answer.answer_text) == "да"
                and DATA_ACCURACY_CONFIRMATION.search(question.question_text)
            )
        return bool(
            fact.state is ConfirmationState.CONFIRMED
            and fact.allow_in_forms
            and (fact.resume_id is None or fact.resume_id == application.resume_id)
            and (fact.direction_id is None or fact.direction_id == application.direction_id)
            and answer.answer_text is not None
            and answer.answer_text.strip() == fact.content.strip()
            and cls._fact_is_current(
                fact,
                question.question_text,
                policy,
                now=now,
            )
        )

    def mark_sent(
        self,
        application_id: int,
        *,
        version_hash: str | None = None,
        sent_at: datetime | None = None,
    ) -> None:
        statement = select(ScreeningFormModel).where(
            ScreeningFormModel.application_id == application_id,
            ScreeningFormModel.state.not_in(
                (ScreeningFormState.INVALIDATED, ScreeningFormState.SENT)
            ),
        )
        if version_hash is not None:
            statement = statement.where(ScreeningFormModel.version_hash == version_hash)
        form = self._session.scalar(
            statement.order_by(
                ScreeningFormModel.updated_at.desc(),
                ScreeningFormModel.id.desc(),
            ).limit(1)
        )
        if form is None:
            return
        form.state = ScreeningFormState.SENT
        form.confirmed_at = sent_at or datetime.now(UTC)
        self._session.flush()

    def _draft(self, form: ScreeningFormModel) -> ScreeningDraft:
        application = self._session.get(ApplicationModel, form.application_id)
        if application is None:
            raise RuntimeError("Отклик черновика отсутствует")
        vacancy = self._session.get(VacancyModel, application.vacancy_id)
        resume = self._session.get(ResumeModel, application.resume_id)
        if vacancy is None or resume is None:
            raise RuntimeError("Вакансия или резюме черновика отсутствуют")
        rows = self._session.execute(
            select(ScreeningQuestionModel, ScreeningAnswerModel)
            .outerjoin(
                ScreeningAnswerModel,
                ScreeningAnswerModel.question_id == ScreeningQuestionModel.id,
            )
            .where(ScreeningQuestionModel.form_id == form.id)
            .order_by(ScreeningQuestionModel.position, ScreeningQuestionModel.id)
        )
        questions = tuple(
            ScreeningDraftQuestion(
                field_key=question.field_key,
                question=question.question_text,
                field_type=question.field_type,
                is_required=question.is_required,
                options=tuple(question.options),
                answer=answer.answer_text if answer is not None else None,
                source=answer.source if answer is not None else None,
            )
            for question, answer in rows
        )
        cover_letter = self._session.scalar(
            select(CoverLetterModel.text)
            .where(
                CoverLetterModel.application_id == application.id,
                CoverLetterModel.state == CoverLetterState.READY,
                CoverLetterModel.text.is_not(None),
                CoverLetterModel.instruction_version
                == cover_letter_instruction_version(
                    AiPromptSettingsService(self._session).get().cover_letter
                ),
            )
            .order_by(CoverLetterModel.id.desc())
            .limit(1)
        )
        return ScreeningDraft(
            form_id=form.id,
            application_id=application.id,
            vacancy_id=vacancy.hh_id,
            vacancy_title=vacancy.title,
            company=vacancy.employer_name or "Компания не указана",
            source_url=vacancy.source_url,
            resume_hh_id=resume.hh_id,
            resume_title=resume.title,
            version_hash=form.version_hash,
            state=form.state,
            questions=questions,
            cover_letter=cover_letter,
        )

    def _templates(
        self,
        profile_id: int,
        application: ApplicationModel,
    ) -> tuple[tuple[AnswerTemplateModel, VerifiedFactModel | None], ...]:
        rows = self._session.execute(
            select(AnswerTemplateModel, VerifiedFactModel)
            .outerjoin(
                VerifiedFactModel,
                VerifiedFactModel.id == AnswerTemplateModel.verified_fact_id,
            )
            .where(
                AnswerTemplateModel.profile_id == profile_id,
                AnswerTemplateModel.is_active.is_(True),
                (
                    (VerifiedFactModel.id.is_(None))
                    | (
                        (VerifiedFactModel.resume_id.is_(None))
                        | (VerifiedFactModel.resume_id == application.resume_id)
                    )
                ),
                (
                    (VerifiedFactModel.id.is_(None))
                    | (
                        (VerifiedFactModel.direction_id.is_(None))
                        | (VerifiedFactModel.direction_id == application.direction_id)
                    )
                ),
            )
            .order_by(AnswerTemplateModel.id)
        )
        return tuple((template, cast(VerifiedFactModel | None, fact)) for template, fact in rows)

    def _facts(
        self,
        profile_id: int,
        application: ApplicationModel,
    ) -> tuple[VerifiedFactModel, ...]:
        return tuple(
            self._session.scalars(
                select(VerifiedFactModel)
                .where(
                    VerifiedFactModel.profile_id == profile_id,
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                    VerifiedFactModel.allow_in_forms.is_(True),
                    (
                        (VerifiedFactModel.resume_id.is_(None))
                        | (VerifiedFactModel.resume_id == application.resume_id)
                    ),
                    (
                        (VerifiedFactModel.direction_id.is_(None))
                        | (VerifiedFactModel.direction_id == application.direction_id)
                    ),
                )
                .order_by(VerifiedFactModel.id)
            )
        )

    def _resolve(
        self,
        field: HhScreeningField,
        templates: tuple[tuple[AnswerTemplateModel, VerifiedFactModel | None], ...],
        facts: tuple[VerifiedFactModel, ...],
        *,
        policy: AutonomyPolicy,
        now: datetime,
    ) -> _ResolvedAnswer | None:
        if (
            field.has_attachment
            or field.has_external_action
            or field.has_test_assignment
            or DANGEROUS_QUESTION.search(field.question)
        ):
            return None
        if DATA_ACCURACY_CONFIRMATION.search(field.question):
            answer = self._compatible_answer(field, "Да")
            if answer is not None:
                return _ResolvedAnswer(answer, AnswerSource.PROFILE, None, True)
        normalized_question = self._normalize(field.question)
        for template, fact in templates:
            fact_allowed = fact is not None and (
                fact.state == ConfirmationState.CONFIRMED
                and fact.allow_in_forms
                and self._fact_is_current(
                    fact,
                    field.question,
                    policy,
                    now=now,
                )
            )
            if self._normalize(template.question_pattern) == normalized_question:
                answer = self._compatible_answer(field, template.answer_text)
                if answer is not None:
                    return _ResolvedAnswer(
                        answer,
                        AnswerSource.BANK,
                        template.verified_fact_id,
                        fact_allowed,
                    )

        category = self._question_key(field.question) or self._fact_category(field.question)
        if category is None:
            return None
        for fact in facts:
            if fact.category != category:
                continue
            answer = self._compatible_answer(field, fact.content)
            if answer is not None:
                return _ResolvedAnswer(
                    answer,
                    AnswerSource.PROFILE,
                    fact.id,
                    self._fact_is_current(
                        fact,
                        field.question,
                        policy,
                        now=now,
                    ),
                )
        return None

    @classmethod
    def _fact_is_current(
        cls,
        fact: VerifiedFactModel,
        question: str,
        policy: AutonomyPolicy,
        *,
        now: datetime,
    ) -> bool:
        categories = {
            fact.category,
            cls._question_key(question),
            cls._fact_category(question),
        }
        if MUTABLE_FACT_CATEGORIES.isdisjoint(categories):
            return True
        if fact.actual_at is None:
            return False
        threshold = as_utc(now) - timedelta(days=policy.mutable_fact_validity_days)
        return as_utc(fact.actual_at) >= threshold

    def _form_for_account(self, account_id: int, form_id: int) -> ScreeningFormModel:
        form = self._session.scalar(
            select(ScreeningFormModel)
            .join(ApplicationModel, ApplicationModel.id == ScreeningFormModel.application_id)
            .where(
                ScreeningFormModel.id == form_id,
                ApplicationModel.account_id == account_id,
            )
        )
        if form is None:
            raise LookupError("Черновик анкеты не найден")
        return form

    def _save_answer_template(
        self,
        application: ApplicationModel,
        question: str,
        answer: str,
    ) -> VerifiedFactModel:
        profile = self._session.scalar(
            select(CandidateProfileModel).where(
                CandidateProfileModel.account_id == application.account_id
            )
        )
        if profile is None:
            raise LookupError("Профиль кандидата не найден; сначала импортируйте резюме")
        digest = hashlib.sha256(self._normalize(question).encode("utf-8")).hexdigest()[:24]
        scope = f"{application.direction_id or 0}:{application.resume_id}"
        key = f"screening:{scope}:{digest}"
        category = (
            self._question_key(question) or self._fact_category(question) or "screening_answer"
        )
        fact = self._session.scalar(
            select(VerifiedFactModel).where(
                VerifiedFactModel.profile_id == profile.id,
                VerifiedFactModel.source_reference == key,
            )
        )
        if fact is None:
            fact = VerifiedFactModel(
                profile_id=profile.id,
                category=category,
                source_type="user",
                source_reference=key,
                resume_id=application.resume_id,
                direction_id=application.direction_id,
            )
            self._session.add(fact)
        fact.category = category
        fact.content = answer
        fact.actual_at = datetime.now(UTC)
        fact.state = ConfirmationState.CONFIRMED
        fact.allow_in_forms = True
        self._session.flush()

        template = self._session.scalar(
            select(AnswerTemplateModel).where(
                AnswerTemplateModel.profile_id == profile.id,
                AnswerTemplateModel.key == key,
            )
        )
        if template is None:
            template = AnswerTemplateModel(profile_id=profile.id, key=key)
            self._session.add(template)
        template.question_pattern = question
        template.answer_text = answer
        template.verified_fact_id = fact.id
        template.is_active = True
        self._session.flush()
        return fact

    def _resume_task(self, application_id: int, selected_at: datetime) -> None:
        task = self._session.scalar(
            select(ApplicationTaskModel).where(
                ApplicationTaskModel.application_id == application_id
            )
        )
        if task is None:
            return
        tasks = QueueTaskRepository(self._session)
        if task.state is TaskState.INPUT_REQUIRED:
            tasks.transition(
                task.id,
                TaskState.REVIEW_REQUIRED,
                error_code="FORM_ANSWERS_CONFIRMED",
            )
            task = self._session.get(ApplicationTaskModel, task.id)
            if task is None:
                return
        if task.state is TaskState.REVIEW_REQUIRED:
            tasks.transition(
                task.id,
                TaskState.RETRY_SCHEDULED,
                scheduled_at=selected_at,
                error_code=FORM_PREFLIGHT_PASSED,
            )

    def _move_task_to_review(self, application_id: int) -> None:
        task = self._session.scalar(
            select(ApplicationTaskModel).where(
                ApplicationTaskModel.application_id == application_id
            )
        )
        if task is None or task.state is not TaskState.INPUT_REQUIRED:
            return
        QueueTaskRepository(self._session).transition(
            task.id,
            TaskState.REVIEW_REQUIRED,
            error_code="FORM_ANSWERS_CONFIRMED",
        )

    @classmethod
    def _simple_structure(cls, form: HhScreeningForm) -> bool:
        return (
            bool(form.fields)
            and not form.warnings
            and all(cls._simple_field(field) for field in form.fields)
        )

    @classmethod
    def _simple_field(cls, field: HhScreeningField) -> bool:
        if cls._prohibited(field):
            return False
        if field.field_type.casefold() not in SUPPORTED_AUTOMATIC_FIELD_TYPES:
            return False
        if SERIOUS_QUESTION.search(field.question) or SERIOUS_OBLIGATION.search(field.question):
            return False
        return (
            DATA_ACCURACY_CONFIRMATION.search(field.question) is not None
            or cls._question_key(field.question) is not None
            or cls._fact_category(field.question) is not None
        )

    @staticmethod
    def _prohibited(field: HhScreeningField) -> bool:
        return bool(
            field.has_attachment
            or field.has_external_action
            or field.has_test_assignment
            or DANGEROUS_QUESTION.search(field.question)
            or SERIOUS_OBLIGATION.search(field.question)
        )

    @staticmethod
    def _stored_field(question: ScreeningQuestionModel) -> HhScreeningField:
        return HhScreeningField(
            key=question.field_key,
            question=question.question_text,
            field_type=question.field_type,
            is_required=question.is_required,
            options=tuple(question.options),
            max_length=question.max_length,
            format_hint=question.format_hint or "",
            has_attachment=question.has_attachment,
            has_external_action=question.has_external_action,
            has_test_assignment=question.has_test_assignment,
        )

    @staticmethod
    def _compatible_answer(field: HhScreeningField, value: str) -> str | None:
        answer = value.strip()
        if not answer or (field.max_length is not None and len(answer) > field.max_length):
            return None
        field_type = field.field_type.casefold()
        if field_type == "checkbox":
            normalized = ScreeningDraftService._normalize(answer)
            if normalized in {"да", "true", "1", "согласен"}:
                return "Да"
            if normalized in {"нет", "false", "0", "не согласен"}:
                return "Нет"
            return None
        if field.options:
            normalized = ScreeningDraftService._normalize(answer)
            return next(
                (
                    option
                    for option in field.options
                    if ScreeningDraftService._normalize(option) == normalized
                ),
                None,
            )
        if (
            field_type == "email"
            and re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+",
                answer,
            )
            is None
        ):
            return None
        if field_type == "tel":
            digits = re.sub(r"\D", "", answer)
            if not 7 <= len(digits) <= 15 or re.fullmatch(r"[\d\s()+-]+", answer) is None:
                return None
        if field_type == "url" and re.fullmatch(r"https?://\S+", answer) is None:
            return None
        if field_type == "number" and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", answer) is None:
            return None
        if field_type == "date":
            try:
                date.fromisoformat(answer)
            except ValueError:
                return None
        return answer

    @staticmethod
    def _question_key(question: str) -> str | None:
        for key, patterns in QUESTION_KEYS:
            if any(pattern.search(question) for pattern in patterns):
                return key
        return None

    @staticmethod
    def _fact_category(question: str) -> str | None:
        for category, patterns in FACT_PATTERNS:
            if any(pattern.search(question) for pattern in patterns):
                return category
        return None

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().replace("ё", "е").split())
