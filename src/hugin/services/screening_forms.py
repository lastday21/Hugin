from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    AnswerTemplateModel,
    ApplicationModel,
    CandidateProfileModel,
    CoverLetterModel,
    ResumeModel,
    ScreeningAnswerModel,
    ScreeningFormModel,
    ScreeningQuestionModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.content import (
    AnswerSource,
    ConfirmationState,
    CoverLetterState,
    ScreeningFormState,
)
from hugin.domain.hh import HhScreeningField, HhScreeningForm, screening_form_hash


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
)

DANGEROUS_QUESTION = re.compile(
    r"паспорт|банк|кар[тт]ы|код\s+(?:из|подтверждения)|смс|sms|оплат|"
    r"установ.*программ|испытательн|тестов.*задан",
    re.IGNORECASE,
)


class ScreeningDraftService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def capture(
        self,
        application_id: int,
        form: HhScreeningForm,
    ) -> ScreeningDraft:
        application = self._session.get(ApplicationModel, application_id)
        if application is None:
            raise LookupError("Отклик не найден")
        self._session.execute(
            delete(ScreeningFormModel).where(ScreeningFormModel.application_id == application_id)
        )
        stored = ScreeningFormModel(
            application_id=application_id,
            version_hash=screening_form_hash(form),
            requires_confirmation=True,
            state=ScreeningFormState.DRAFT,
        )
        self._session.add(stored)
        self._session.flush()

        profile = self._session.scalar(
            select(CandidateProfileModel).where(
                CandidateProfileModel.account_id == application.account_id
            )
        )
        templates = self._templates(profile.id) if profile is not None else ()
        facts = self._facts(profile.id) if profile is not None else ()
        required_missing = False
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
            resolved = self._resolve(field, templates, facts)
            if field.is_required and resolved is None:
                required_missing = True
            self._session.add(
                ScreeningAnswerModel(
                    question_id=question.id,
                    answer_text=resolved.text if resolved is not None else None,
                    source=resolved.source if resolved is not None else None,
                    verified_fact_id=(resolved.verified_fact_id if resolved is not None else None),
                )
            )

        stored.state = (
            ScreeningFormState.INPUT_REQUIRED
            if required_missing
            else ScreeningFormState.REVIEW_REQUIRED
        )
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
            .where(
                ApplicationModel.account_id == account_id,
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
            .where(
                ApplicationModel.account_id == account_id,
                VacancyModel.hh_id == vacancy_id,
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

    def invalidate(self, form_id: int) -> None:
        form = self._session.get(ScreeningFormModel, form_id)
        if form is None:
            raise LookupError("Черновик анкеты не найден")
        form.state = ScreeningFormState.INVALIDATED
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
            resume_title=resume.title,
            version_hash=form.version_hash,
            state=form.state,
            questions=questions,
            cover_letter=cover_letter,
        )

    def _templates(
        self,
        profile_id: int,
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
            )
            .order_by(AnswerTemplateModel.id)
        )
        return tuple((template, cast(VerifiedFactModel | None, fact)) for template, fact in rows)

    def _facts(self, profile_id: int) -> tuple[VerifiedFactModel, ...]:
        return tuple(
            self._session.scalars(
                select(VerifiedFactModel)
                .where(
                    VerifiedFactModel.profile_id == profile_id,
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                    VerifiedFactModel.allow_in_forms.is_(True),
                )
                .order_by(VerifiedFactModel.id)
            )
        )

    def _resolve(
        self,
        field: HhScreeningField,
        templates: tuple[tuple[AnswerTemplateModel, VerifiedFactModel | None], ...],
        facts: tuple[VerifiedFactModel, ...],
    ) -> _ResolvedAnswer | None:
        if (
            field.has_attachment
            or field.has_external_action
            or field.has_test_assignment
            or DANGEROUS_QUESTION.search(field.question)
        ):
            return None
        question_key = self._question_key(field.question)
        normalized_question = self._normalize(field.question)
        for template, fact in templates:
            fact_allowed = fact is None or (
                fact.state == ConfirmationState.CONFIRMED and fact.allow_in_forms
            )
            if not fact_allowed:
                continue
            if (
                self._normalize(template.question_pattern) == normalized_question
                or template.key == question_key
            ):
                answer = self._compatible_answer(field, template.answer_text)
                if answer is not None:
                    return _ResolvedAnswer(
                        answer,
                        AnswerSource.BANK,
                        template.verified_fact_id,
                    )

        category = self._fact_category(field.question)
        if category is None:
            return None
        for fact in facts:
            if fact.category != category:
                continue
            answer = self._compatible_answer(field, fact.content)
            if answer is not None:
                return _ResolvedAnswer(answer, AnswerSource.PROFILE, fact.id)
        return None

    @staticmethod
    def _compatible_answer(field: HhScreeningField, value: str) -> str | None:
        answer = value.strip()
        if not answer or (field.max_length is not None and len(answer) > field.max_length):
            return None
        if not field.options:
            return answer
        normalized = ScreeningDraftService._normalize(answer)
        return next(
            (
                option
                for option in field.options
                if ScreeningDraftService._normalize(option) == normalized
            ),
            None,
        )

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
