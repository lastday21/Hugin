from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    AnswerTemplateModel,
    CandidateProfileModel,
    HhAccountModel,
    ProfileQuestionModel,
    ResumeModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState


@dataclass(frozen=True, slots=True)
class UiResume:
    id: int
    hh_id: str
    title: str
    source_type: str | None
    source_original_name: str | None
    source_size_bytes: int | None
    source_page_count: int | None
    imported_at: datetime | None


@dataclass(frozen=True, slots=True)
class UiProfileFact:
    id: int
    category: str
    content: str
    source_type: str
    source_reference: str | None
    state: str
    allow_in_letters: bool
    allow_in_forms: bool
    allow_in_messages: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UiProfileQuestion:
    key: str
    question: str
    answer: str | None
    state: str


@dataclass(frozen=True, slots=True)
class UiAnswerTemplate:
    key: str
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class UiProfile:
    account_label: str
    display_name: str
    active_resume: UiResume | None
    facts: tuple[UiProfileFact, ...]
    questions: tuple[UiProfileQuestion, ...]
    answers: tuple[UiAnswerTemplate, ...]


class UiProfileService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, account_id: int) -> UiProfile:
        account = self._session.get(HhAccountModel, account_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден")
        profile = self._session.scalar(
            select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
        )
        if profile is None:
            return UiProfile(
                account_label=account.label,
                display_name=account.label,
                active_resume=None,
                facts=(),
                questions=(),
                answers=(),
            )

        resume = (
            self._session.get(ResumeModel, profile.active_resume_id)
            if profile.active_resume_id is not None
            else None
        )
        facts = self._session.scalars(
            select(VerifiedFactModel)
            .where(VerifiedFactModel.profile_id == profile.id)
            .order_by(
                case(
                    (VerifiedFactModel.state == ConfirmationState.PENDING, 0),
                    (VerifiedFactModel.state == ConfirmationState.CONFIRMED, 1),
                    else_=2,
                ),
                VerifiedFactModel.id,
            )
        )
        questions = self._session.scalars(
            select(ProfileQuestionModel)
            .where(ProfileQuestionModel.profile_id == profile.id)
            .order_by(ProfileQuestionModel.id)
        )
        answers = self._session.scalars(
            select(AnswerTemplateModel)
            .where(
                AnswerTemplateModel.profile_id == profile.id,
                AnswerTemplateModel.is_active.is_(True),
            )
            .order_by(AnswerTemplateModel.id)
        )
        return UiProfile(
            account_label=account.label,
            display_name=profile.display_name,
            active_resume=self._resume(resume) if resume is not None else None,
            facts=tuple(
                UiProfileFact(
                    id=fact.id,
                    category=fact.category,
                    content=fact.content,
                    source_type=fact.source_type,
                    source_reference=fact.source_reference,
                    state=fact.state.value,
                    allow_in_letters=fact.allow_in_letters,
                    allow_in_forms=fact.allow_in_forms,
                    allow_in_messages=fact.allow_in_messages,
                    created_at=fact.created_at,
                    updated_at=fact.updated_at,
                )
                for fact in facts
            ),
            questions=tuple(
                UiProfileQuestion(
                    key=question.key,
                    question=question.question_text,
                    answer=question.answer_text,
                    state=question.state.value,
                )
                for question in questions
            ),
            answers=tuple(
                UiAnswerTemplate(
                    key=answer.key,
                    question=answer.question_pattern,
                    answer=answer.answer_text,
                )
                for answer in answers
            ),
        )

    @staticmethod
    def _resume(resume: ResumeModel) -> UiResume:
        return UiResume(
            id=resume.id,
            hh_id=resume.hh_id,
            title=resume.title,
            source_type=resume.source_type,
            source_original_name=resume.source_original_name,
            source_size_bytes=resume.source_size_bytes,
            source_page_count=resume.source_page_count,
            imported_at=resume.imported_at,
        )
