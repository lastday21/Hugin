# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    CandidateProfileModel,
    RecruiterMessageFactModel,
    RecruiterMessageModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.communications import (
    CommunicationNotFoundError,
    CommunicationStateError,
)
from hugin.domain.content import ConfirmationState, RecruiterMessageState
from hugin.services.ai_prompts import (
    ALICE_AI_MODEL,
    QWEN3_AI_MODEL,
    AiPromptSettingsService,
)
from hugin.services.communications import CommunicationService, RecordingMessageSender
from hugin.services.recruiter_reply import RecruiterReplyService
from tests.unit.test_communications import create_application

pytestmark = pytest.mark.integration


class FakeReplyModel:
    model_name = "reply-model"

    def __init__(self, response: str = "Здравствуйте! Да, готов обсудить задачи.") -> None:
        self.response = response
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.response


def test_generated_reply_uses_only_allowed_facts_and_remains_draft(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeReplyModel()
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Ответ работодателю",
                vacancy_hh_id="recruiter-reply",
            )
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            vacancy = session.get(VacancyModel, application.vacancy_id)
            assert vacancy is not None
            vacancy.employer_name = "Компания"
            vacancy.responsibilities = "Разрабатывать серверную часть"
            vacancy.required_qualifications = "Python и PostgreSQL"
            vacancy.key_skills = ["Python", "PostgreSQL"]

            profile = CandidateProfileModel(
                account_id=account_id,
                active_resume_id=application.resume_id,
                display_name="Кандидат",
            )
            session.add(profile)
            session.flush()
            allowed = VerifiedFactModel(
                profile_id=profile.id,
                category="relocation",
                content="Рассматривает переезд в крупные города Центральной России.",
                source_type="user",
                state=ConfirmationState.CONFIRMED,
                allow_in_messages=True,
            )
            denied = VerifiedFactModel(
                profile_id=profile.id,
                category="salary",
                content="Ожидает 500 тысяч рублей.",
                source_type="user",
                state=ConfirmationState.PENDING,
                allow_in_messages=True,
            )
            session.add_all((allowed, denied))
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id,
                hh_id="incoming-reply",
                body="Добрый день! Готовы рассмотреть переезд?",
                received_at=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
            )
            prompts = AiPromptSettingsService(session).get()
            AiPromptSettingsService(session).update(
                resume=prompts.resume,
                cover_letter=prompts.cover_letter,
                recruiter_reply="Отвечай тепло, без канцелярских оборотов.",
            )

            draft = RecruiterReplyService(session, model).generate(
                account_id=account_id,
                application_id=application_id,
            )

            assert draft.state is RecruiterMessageState.REVIEW_REQUIRED
            assert draft.body == model.response
            assert draft.confirmed_at is None
            assert "Отвечай тепло" in model.prompts[0][0]
            assert "Центральной России" in model.prompts[0][1]
            assert "500 тысяч" not in model.prompts[0][1]
            assert "Готовы рассмотреть переезд?" in model.prompts[0][1]
            assert tuple(
                session.scalars(
                    select(RecruiterMessageFactModel.fact_id).where(
                        RecruiterMessageFactModel.message_id == draft.id
                    )
                )
            ) == (allowed.id,)

            model.response = "Здравствуйте! Да, готов обсудить переезд подробнее."
            edited = RecruiterReplyService(session, model).generate(
                account_id=account_id,
                application_id=application_id,
            )
            assert edited.id == draft.id
            assert edited.content_version == 2
            assert edited.state is RecruiterMessageState.REVIEW_REQUIRED
    finally:
        database.close()


def test_reply_generation_rejects_missing_context_unknown_result_and_bad_text(
    settings: Settings,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Проверка ответа",
                vacancy_hh_id="recruiter-reply-errors",
            )
            service = RecruiterReplyService(session, FakeReplyModel())
            with pytest.raises(CommunicationStateError, match="дождитесь сообщения"):
                service.generate(account_id=account_id, application_id=application_id)
            with pytest.raises(CommunicationNotFoundError):
                service.generate(account_id=account_id, application_id=99_999)

            communications = CommunicationService(session, RecordingMessageSender())
            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-error",
                body="Когда готовы начать?",
            )
            empty = RecruiterReplyService(session, FakeReplyModel("  "))
            with pytest.raises(ValueError, match="пустой"):
                empty.generate(account_id=account_id, application_id=application_id)
            too_long = RecruiterReplyService(session, FakeReplyModel("x" * 5001))
            with pytest.raises(ValueError, match="длиннее"):
                too_long.generate(account_id=account_id, application_id=application_id)

            draft = communications.create_outgoing_draft(
                application_id=application_id,
                body="Текст с неизвестным результатом",
            )
            stored = session.get(RecruiterMessageModel, draft.id)
            assert stored is not None
            stored.state = RecruiterMessageState.UNKNOWN_RESULT
            session.flush()
            blocked_model = FakeReplyModel()
            with pytest.raises(CommunicationStateError, match="уточните результат"):
                RecruiterReplyService(session, blocked_model).generate(
                    account_id=account_id,
                    application_id=application_id,
                )
            assert blocked_model.prompts == []
    finally:
        database.close()


def test_ai_prompt_settings_validate_update_and_reset(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = AiPromptSettingsService(session)
            defaults = service.get()
            assert service.get_model() == ALICE_AI_MODEL
            assert service.get_reasoning_effort() == "high"
            assert service.update_model(QWEN3_AI_MODEL, "medium") == QWEN3_AI_MODEL
            assert service.get_model() == QWEN3_AI_MODEL
            assert service.get_reasoning_effort() == "medium"
            with pytest.raises(ValueError, match="недоступная модель"):
                service.update_model("unknown/latest")
            with pytest.raises(ValueError, match="режим обработки"):
                service.update_model(QWEN3_AI_MODEL, "unknown")
            updated = service.update(
                resume="  Делай резюме короче.  ",
                cover_letter="Пиши без шаблонов.",
                recruiter_reply="Отвечай по существу.",
            )
            assert updated.resume == "Делай резюме короче."
            assert service.get() == updated
            with pytest.raises(ValueError, match="не может быть пустой"):
                service.update(
                    resume="",
                    cover_letter=updated.cover_letter,
                    recruiter_reply=updated.recruiter_reply,
                )
            with pytest.raises(ValueError, match="не длиннее"):
                service.update(
                    resume=updated.resume,
                    cover_letter="x" * 4001,
                    recruiter_reply=updated.recruiter_reply,
                )
            assert service.reset() == defaults
            assert service.get() == defaults
            assert service.get_model() == QWEN3_AI_MODEL
            assert service.get_reasoning_effort() == "medium"
    finally:
        database.close()
