# ruff: noqa: RUF001

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    CandidateProfileModel,
    CareerDirectionModel,
    RecruiterMessageFactModel,
    RecruiterMessageModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.communications import (
    CommunicationNotFoundError,
    CommunicationStateError,
)
from hugin.domain.content import ConfirmationState, MessageDirection, RecruiterMessageState
from hugin.repositories import ResumeRepository
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
        self.review_payload: dict[str, object] | None = None
        self.reviews: list[str] = []

    def complete_json(self, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> str:
        self.reviews.append(user_prompt)
        missing = "НУЖНО УТОЧНИТЬ" in json.loads(user_prompt)["reply"]
        return json.dumps(
            self.review_payload
            or {
                "supported": True,
                "complete": not missing,
                "questions": ["Есть ли у вас коммерческий опыт в финтехе?"] if missing else [],
                "reason": "Проверено по фактам",
            },
            ensure_ascii=False,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.response


def test_unsupported_denial_gets_one_correction_to_candidate_question(settings: Settings) -> None:
    class CorrectingModel(FakeReplyModel):
        def complete(self, system_prompt: str, user_prompt: str) -> str:
            super().complete(system_prompt, user_prompt)
            if len(self.prompts) == 1:
                return "Коммерческого опыта именно в финтехе у меня не было."
            return "[НУЖНО УТОЧНИТЬ: есть ли у вас коммерческий опыт в финтехе?]"

    upgrade_database(settings)
    database = create_database(settings)
    model = CorrectingModel()
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session, account_label="Уточнение опыта", vacancy_hh_id="reply-clarify"
            )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id,
                hh_id="clarify",
                body="У вас есть опыт работы в fintech",
            )
            draft = RecruiterReplyService(session, model).generate(
                account_id=account_id, application_id=application_id
            )
            assert draft.body.startswith("[НУЖНО УТОЧНИТЬ:")
            assert draft.state is RecruiterMessageState.REVIEW_REQUIRED
            assert not draft.auto_send_approved
            assert len(model.prompts) == 2
            assert "Отрицание опыта не подтверждено" in model.prompts[1][1]
    finally:
        database.close()


@pytest.mark.parametrize("supported", [True, False])
def test_review_detects_invented_experience_and_missing_answer(
    settings: Settings,
    supported: bool,
) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeReplyModel("Разрабатывал коммерческие системы на Java пять лет.")
    model.review_payload = {
        "supported": supported,
        "complete": False,
        "questions": ["Сколько часов в неделю вы готовы работать?"],
        "reason": "Проверьте опыт и часы работы",
    }
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session, account_label="Проверка полноты", vacancy_hh_id="reply-review"
            )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id,
                hh_id="review",
                body="Какой опыт Java и сколько часов в неделю готовы работать?",
            )
            service = RecruiterReplyService(session, model)
            if supported:
                reviewed = service.generate_reviewed(
                    account_id=account_id, application_id=application_id
                )
                draft = reviewed.message
                assert reviewed.review.supported
                assert not reviewed.review.complete
                assert reviewed.review.questions == ("Сколько часов в неделю вы готовы работать?",)
                assert "[НУЖНО УТОЧНИТЬ: Сколько часов" in draft.body
                assert not draft.auto_send_approved
                assert len(model.prompts) == 1
            else:
                with pytest.raises(ValueError, match="не подтверждён"):
                    service.generate(account_id=account_id, application_id=application_id)
                assert len(model.prompts) == 2
                assert len(model.reviews) == 2
    finally:
        database.close()


@pytest.mark.parametrize("already_marked", [False, True])
def test_quoted_unanswered_question_still_requires_candidate_clarification(
    settings: Settings,
    already_marked: bool,
) -> None:
    database = create_database(settings)
    question = "What is your level of English?"
    marker = f"[НУЖНО УТОЧНИТЬ: {question}]"
    model = FakeReplyModel(
        marker
        if already_marked
        else f'Hello! Regarding "{question}", I would like to discuss this.'
    )
    model.review_payload = {
        "supported": True,
        "complete": False,
        "questions": [question],
        "reason": "The answer does not state the English level",
    }
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session,
                account_label="Quoted question",
                vacancy_hh_id="quoted-question",
            )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id,
                hh_id="quoted-question",
                body=question,
            )
            draft = RecruiterReplyService(session, model).generate(
                account_id=account_id,
                application_id=application_id,
            )
            assert draft.body.count(marker) == 1
            assert draft.state is RecruiterMessageState.REVIEW_REQUIRED
            assert not draft.auto_send_approved
    finally:
        database.close()


def test_reply_context_keeps_last_question_and_fact_qualifications() -> None:
    vacancy = VacancyModel(title="Developer", key_skills=[])
    question = "First question. " * 120 + "Final question about weekly hours?"
    fact = VerifiedFactModel(
        category="project", content="Implemented Python services. " * 80 + "Personal project only."
    )
    message = RecruiterMessageModel(direction=MessageDirection.INCOMING, body=question)
    prompt = RecruiterReplyService._prompt(vacancy, (message,), (fact,))
    assert "Final question about weekly hours?" in prompt
    assert "Personal project only." in prompt


@pytest.mark.parametrize("scoped_direction", [False, True])
def test_reply_facts_respect_application_scope(settings: Settings, scoped_direction: bool) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeReplyModel()
    try:
        with database.sessions.begin() as session:
            account_id, application_id = create_application(
                session, account_label="Область фактов", vacancy_hh_id="reply-scope"
            )
            application = session.get(ApplicationModel, application_id)
            assert application is not None
            other_resume = ResumeRepository(session).upsert(account_id, "other", "Другое резюме")
            direction = CareerDirectionModel(account_id=account_id, name="Основное")
            other_direction = CareerDirectionModel(account_id=account_id, name="Другое")
            session.add_all((direction, other_direction))
            session.flush()
            application.direction_id = direction.id if scoped_direction else None
            profile = CandidateProfileModel(account_id=account_id, display_name="Кандидат")
            session.add(profile)
            session.flush()
            facts = [
                VerifiedFactModel(
                    profile_id=profile.id,
                    category="experience",
                    content=content,
                    source_type="user",
                    state=ConfirmationState.CONFIRMED,
                    allow_in_messages=True,
                    resume_id=resume_id,
                    direction_id=direction_id,
                )
                for content, resume_id, direction_id in (
                    ("Общий проверенный факт", None, None),
                    (
                        "Подходящий проверенный факт",
                        application.resume_id,
                        application.direction_id,
                    ),
                    ("Факт другого резюме", other_resume.id, None),
                    ("Факт другого направления", None, other_direction.id),
                )
            ]
            session.add_all(facts)
            session.flush()
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=application_id, hh_id="scope", body="Расскажите о вашем опыте?"
            )
            draft = RecruiterReplyService(session, model).generate(
                account_id=account_id, application_id=application_id
            )
            assert "Общий проверенный факт" in model.prompts[0][1]
            assert "Подходящий проверенный факт" in model.prompts[0][1]
            assert "Факт другого" not in model.prompts[0][1]
            assert "Факт другого" not in model.reviews[0]
            assert set(
                session.scalars(
                    select(RecruiterMessageFactModel.fact_id).where(
                        RecruiterMessageFactModel.message_id == draft.id
                    )
                )
            ) == {facts[0].id, facts[1].id}
    finally:
        database.close()


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
            session.add_all(
                VerifiedFactModel(
                    profile_id=profile.id,
                    category=category,
                    content="Ответ без контекста из прежней анкеты",
                    source_type="user",
                    source_reference=reference,
                    state=ConfirmationState.CONFIRMED,
                    allow_in_messages=True,
                )
                for category, reference in (
                    ("screening_answer", None),
                    ("experience", "screening:17"),
                )
            )
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
            assert "Ответ без контекста" not in model.prompts[0][1]
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
                body="К сожалению, сейчас мы не готовы пригласить вас дальше.",
            )
            refusal_model = FakeReplyModel()
            with pytest.raises(CommunicationStateError, match="отвечать не нужно"):
                RecruiterReplyService(session, refusal_model).generate(
                    account_id=account_id,
                    application_id=application_id,
                )
            assert refusal_model.prompts == []

            communications.save_incoming(
                application_id=application_id,
                hh_id="incoming-question",
                body="Когда готовы начать?",
            )
            empty = RecruiterReplyService(session, FakeReplyModel("  "))
            with pytest.raises(ValueError, match="пустой"):
                empty.generate(account_id=account_id, application_id=application_id)
            too_long = RecruiterReplyService(session, FakeReplyModel("x" * 5001))
            with pytest.raises(ValueError, match="длиннее"):
                too_long.generate(account_id=account_id, application_id=application_id)

            unsupported = RecruiterReplyService(
                session, FakeReplyModel("Прямого опыта в fintech у меня пока нет.")
            )
            with pytest.raises(ValueError, match="Отрицание опыта не подтверждено"):
                unsupported.generate(account_id=account_id, application_id=application_id)

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
