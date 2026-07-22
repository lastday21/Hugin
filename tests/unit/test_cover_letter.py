from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    CandidateProfileModel,
    CoverLetterFactModel,
    CoverLetterModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState, CoverLetterState
from hugin.domain.directions import VacancyState
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, DirectionRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.cover_letter import (
    MAX_LETTER_LENGTH,
    CoverLetterService,
    CoverLetterValidationError,
    _relevant_excerpt,
    _SelectedFact,
    _work_experience_excerpt,
    build_cover_letter_prompt,
    normalize_cover_letter,
    validate_cover_letter,
)

pytestmark = pytest.mark.integration


class FakeModel:
    model_name = "yandexgpt-test"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        return self.responses.pop(0)


def _letter() -> str:
    return (
        "Здравствуйте!\n\n"
        "Разрабатывал серверные приложения на Python с FastAPI и PostgreSQL, поэтому знаком "
        "с задачами развития серверной части и интеграций. В одном из проектов реализовал "
        "прикладную логику сервиса и настроил автоматические проверки, чтобы изменения можно "
        "было безопасно проверять перед выпуском. Этот опыт позволит быстро включиться в "
        "похожие задачи и разбираться в существующем коде.\n\n"
        "Буду рад подробнее обсудить задачи команды и рассказать о реализованных решениях."
    )


def _prepare_data(
    session: object,
    *,
    with_duplicate: bool = False,
) -> tuple[int, int, int, tuple[int, ...]]:
    account = AccountRepository(session).create("Кандидат", "account-letters")  # type: ignore[arg-type]
    resume = ResumeRepository(session).upsert(  # type: ignore[arg-type]
        account.id,
        "resume-letters",
        "Python-разработчик",
    )
    directions = DirectionRepository(session)  # type: ignore[arg-type]
    direction = directions.create(account.id, "Python backend")
    directions.attach_resume(direction.id, resume.id)
    vacancies = VacancyRepository(session)  # type: ignore[arg-type]
    first = vacancies.upsert(
        VacancyData(
            hh_id="letter-1",
            title="Python-разработчик",
            source_url="https://hh.ru/vacancy/letter-1",
            employer_name="Тестовая компания",
            published_at=datetime(2026, 7, 22, tzinfo=UTC),
            description="Полное описание: FastAPI, PostgreSQL, интеграции и проверка кода.",
            responsibilities="Развивать серверную часть и интеграции.",
            required_qualifications="Python, FastAPI, PostgreSQL.",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
            details_fetched_at=datetime(2026, 7, 22, tzinfo=UTC),
        )
    )
    stored = [first]
    if with_duplicate:
        duplicate = vacancies.upsert(
            VacancyData(
                hh_id="letter-2",
                title="Python-разработчик",
                source_url="https://hh.ru/vacancy/letter-2",
                employer_name="Тестовая компания",
                published_at=datetime(2026, 7, 23, tzinfo=UTC),
                description="Повторная публикация: FastAPI, PostgreSQL и интеграции.",
                key_skills=("Python", "FastAPI", "PostgreSQL"),
                details_fetched_at=datetime(2026, 7, 23, tzinfo=UTC),
            )
        )
        vacancies.mark_duplicate(duplicate.id, first.id, 0.95)
        stored.append(duplicate)

    for vacancy in stored:
        directions.track_vacancy(direction.id, vacancy.id)
        directions.apply_rules(
            direction.id,
            vacancy.id,
            state=VacancyState.ANALYZED,
            score=85,
            details={
                "category": "MATCH",
                "accepted": True,
                "reasons": ["совпадают Python, FastAPI и PostgreSQL"],
            },
        )

    profile = CandidateProfileModel(
        account_id=account.id,
        active_resume_id=resume.id,
        display_name="Кандидат",
    )
    session.add(profile)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    allowed = VerifiedFactModel(
        profile_id=profile.id,
        category="work_experience",
        content=(
            "Разрабатывал серверные приложения на Python. Работал с FastAPI и PostgreSQL. "
            "Настраивал автоматические проверки.\nGitHub: github.com/candidate"
        ),
        source_type="resume",
        resume_id=resume.id,
        state=ConfirmationState.CONFIRMED,
        allow_in_letters=True,
    )
    denied = VerifiedFactModel(
        profile_id=profile.id,
        category="work_experience",
        content="Руководил командой и работал с Kubernetes.",
        source_type="resume",
        resume_id=resume.id,
        state=ConfirmationState.PENDING,
        allow_in_letters=False,
    )
    session.add_all((allowed, denied))  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    ApplicationAutomationService(session).prepare_for_account_id(  # type: ignore[arg-type]
        account_id=account.id,
        direction_name=direction.name,
        include_stretch=True,
    )
    return account.id, direction.id, resume.id, tuple(item.id for item in stored)


def test_yandex_letter_uses_only_confirmed_facts_and_is_saved(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, _ = _prepare_data(session)
            before = CoverLetterService(session).status(
                account_id=account_id,
                direction_name="Python backend",
            )
            assert before.missing == 1
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.generated == 1
            assert result.failed == 0
            assert "Полное описание" in model.prompts[0][1]
            assert "Настраивал автоматические проверки" in model.prompts[0][1]
            assert "Kubernetes" not in model.prompts[0][1]
            assert "github.com" not in model.prompts[0][1]

            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state == CoverLetterState.READY
            assert letter.text == _letter()
            assert letter.context_hash
            fact_ids = tuple(
                session.scalars(
                    select(CoverLetterFactModel.fact_id).where(
                        CoverLetterFactModel.cover_letter_id == letter.id
                    )
                )
            )
            assert len(fact_ids) == 1
            after = CoverLetterService(session).status(
                account_id=account_id,
                direction_name="Python backend",
            )
            assert after.ready == 1
            assert after.missing == 0
            repeated = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=1,
            )
            assert repeated.already_ready == 1
            assert len(model.prompts) == 1

            job = ApplicationAutomationService(session).claim_next(
                direction_id,
                require_cover_letter=True,
            )
            assert job is not None
            assert job.cover_letter == _letter()
            ApplicationAutomationService(session).record_result(
                job,
                HhApplyResult(HhApplyStatus.APPLIED, job.vacancy.source_url, "успешно"),
            )
            sent_letter = session.get(CoverLetterModel, letter.id)
            assert sent_letter is not None
            assert sent_letter.state == CoverLetterState.SENT
            assert sent_letter.sent_at is not None
    finally:
        database.close()


def test_unconfirmed_number_fails_without_fallback(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel(
        [
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня 5 лет опыта, поэтому "
            "задачи серверной разработки хорошо знакомы. Также реализовывал прикладную логику "
            "и интеграции. Буду рад подробнее рассказать о проектах и обсудить задачи команды."
        ]
    )
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, _, _ = _prepare_data(session)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.failed == 1
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state is CoverLetterState.FAILED
            assert letter.text is None
            assert letter.failure_reason == "UNCONFIRMED_NUMBER"
            assert (
                ApplicationAutomationService(session).claim_next(
                    direction_id,
                    require_cover_letter=True,
                )
                is None
            )
    finally:
        database.close()


def test_related_publication_reuses_ready_letter(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session, with_duplicate=True)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.generated == 1
            assert result.reused == 1
            assert len(model.prompts) == 1
            letters = list(session.scalars(select(CoverLetterModel).order_by(CoverLetterModel.id)))
            assert len(letters) == 2
            assert letters[1].text == letters[0].text
            assert letters[1].reused_from_id == letters[0].id
    finally:
        database.close()


def test_prepare_can_target_exactly_one_vacancy(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    model = FakeModel([_letter()])
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session, with_duplicate=True)
            result = CoverLetterService(session, model).prepare(
                account_id=account_id,
                direction_name="Python backend",
                limit=20,
                vacancy_hh_id="letter-1",
            )

            assert result.generated == 1
            assert len(result.items) == 1
            assert result.items[0].hh_id == "letter-1"
            assert len(model.prompts) == 1

            with pytest.raises(LookupError, match="missing"):
                CoverLetterService(session, model).prepare(
                    account_id=account_id,
                    direction_name="Python backend",
                    vacancy_hh_id="missing",
                )
            assert len(model.prompts) == 1
    finally:
        database.close()


class FailingModel:
    model_name = "yandexgpt-test"

    def complete(self, _system_prompt: str, _user_prompt: str) -> str:
        from hugin.adapters.yandex_ai import YandexAIError

        raise YandexAIError("временная ошибка")


def test_yandex_failure_is_saved_without_common_letter(settings: Settings) -> None:
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account_id, _, _, _ = _prepare_data(session)
            result = CoverLetterService(session, FailingModel()).prepare(
                account_id=account_id,
                direction_name="Python backend",
            )

            assert result.failed == 1
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            assert letter.state == CoverLetterState.FAILED
            assert letter.text is None
            assert letter.failure_reason == "YANDEXGPT_ERROR"
    finally:
        database.close()


def _vacancy() -> VacancyModel:
    return VacancyModel(
        id=10,
        hh_id="validation",
        title="Python-разработчик",
        source_url="https://hh.ru/vacancy/validation",
        employer_name="Тестовая компания",
        key_skills=["Python"],
    )


def _fact() -> tuple[_SelectedFact, ...]:
    return (
        _SelectedFact(
            id=1,
            category="work_experience",
            content="Разрабатывал серверные приложения на Python и FastAPI.",
        ),
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "EMPTY"),
        ("Слишком коротко", "TOO_SHORT"),
        ("Я" * (MAX_LETTER_LENGTH + 1), "TOO_LONG"),
        (
            "Вот готовое письмо: заинтересовала вакансия Python-разработчика в Тестовой "
            "компании. Подтвержден опыт с Python и FastAPI. Готов обсудить задачи команды.",
            "SERVICE_TEXT",
        ),
        (
            "Здравствуйте!\n\nЗаинтересовала вакансия Python-разработчика в Тестовой компании. "
            "Подтвержден опыт с Python и FastAPI. [Укажите здесь достижение.] Буду рад "
            "обсудить задачи команды.",
            "PLACEHOLDER",
        ),
        (
            "Разрабатывал серверные приложения на Python и FastAPI, работал с PostgreSQL и "
            "настраивал автоматические проверки. Этот опыт связан с задачами развития "
            "серверной части и интеграций. Буду рад подробнее рассказать о реализованных "
            "решениях и обсудить задачи команды.",
            "MISSING_GREETING",
        ),
        (
            "Здравствуйте!\n\nВижу, что вы ищете разработчика серверных приложений. "
            "Разрабатывал сервисы на Python и FastAPI, работал с PostgreSQL и настраивал "
            "автоматические проверки. Этот опыт связан с развитием серверной части и "
            "интеграциями. Буду рад подробнее рассказать о выполненных проектах и обсудить "
            "задачи команды на собеседовании.",
            "TEMPLATE_PHRASE",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Также интегрировал внешние "
            "сервисы через API. Этот опыт использовал при создании AI-агентов и RAG-модулей. "
            "Буду рад подробнее рассказать о выполненных проектах и обсудить задачи команды "
            "на собеседовании.",
            "UNCONFIRMED_SPECIALIST_TERM",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня 5 лет опыта. Также "
            "реализовывал прикладную логику и интеграции. Буду рад подробнее рассказать о "
            "проектах и обсудить задачи команды.",
            "UNCONFIRMED_NUMBER",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. У меня пять лет опыта. Также "
            "реализовывал прикладную логику и интеграции. Буду рад подробнее рассказать о "
            "проектах и обсудить задачи команды.",
            "UNCONFIRMED_EXPERIENCE",
        ),
        (
            "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
            "с PostgreSQL и настраивал автоматические проверки. Ранее сотрудничал с компанией "
            "«Чужая». Также реализовывал прикладную логику и интеграции. Буду рад подробнее "
            "рассказать о проектах и обсудить задачи команды.",
            "OTHER_EMPLOYER",
        ),
    ],
)
def test_objective_letter_validation(text: str, code: str) -> None:
    with pytest.raises(CoverLetterValidationError) as error:
        validate_cover_letter(text, _vacancy(), _fact())
    assert error.value.code == code


def test_prompt_normalization_and_context_selection() -> None:
    assert normalize_cover_letter('```text\n"Готовый текст"\n```') == "Готовый текст"
    assert normalize_cover_letter("«Еще один текст»") == "Еще один текст"

    long_context = (
        "Общая строка без совпадения.\n" * 20
        + "Разрабатывал интеграции на FastAPI и Python.\n"
        + "PostgreSQL использовал для хранения данных."
    )
    excerpt = _relevant_excerpt(long_context, {"python", "fastapi"}, 150)
    assert "FastAPI" in excerpt
    fallback = _relevant_excerpt("Оченьдлиннаястрока" * 20, {"python"}, 20)
    assert fallback
    minimal = _relevant_excerpt(
        "ООО Предыдущий работодатель\nРазрабатывал сервис на Python\nОбщая информация",
        {"python"},
        200,
        minimal=True,
    )
    assert "Предыдущий работодатель" not in minimal
    assert "Разрабатывал сервис" in minimal

    prompt = build_cover_letter_prompt(
        _vacancy(),
        "Python backend",
        None,
        _fact(),
    )
    assert "Причины совпадения отдельно не выделены" in prompt
    assert "Полное описание" not in prompt
    assert "не повторяй название вакансии и компании" in prompt
    assert "1–2 наиболее подходящих проекта" in prompt
    assert "не смешивай сведения разных должностей и проектов" in prompt
    assert "нет требуемой технологии" in prompt
    assert "Здравствуйте!" in prompt


def test_work_experience_context_keeps_project_boundaries() -> None:
    content = """Январь 2024 — настоящее время
1 год
Компания
Разработчик
- Разрабатывал сервисы на Python.
Проекты:
- Цифровой подкастер — генерировал аудио из текста через SpeechKit.
- Аналитик — собирал новости и формировал отчет через LLM.
Стек: Python, LLM, SpeechKit."""

    excerpt = _work_experience_excerpt(
        content,
        {"python", "llm", "speechkit", "новости"},
        3000,
    )

    assert '<experience_item type="PROJECT" label="Цифровой подкастер">' in excerpt
    assert "генерировал аудио из текста через SpeechKit" in excerpt
    assert '<experience_item type="PROJECT" label="Аналитик">' in excerpt
    assert "собирал новости и формировал отчет через LLM" in excerpt


def test_specialist_term_boundaries_do_not_match_storage() -> None:
    text = (
        "Здравствуйте!\n\nРазрабатывал серверные приложения на Python и FastAPI, работал "
        "с PostgreSQL и настраивал автоматические проверки. Для storage-слоя применял "
        "PostgreSQL, а прикладную логику отделял от доступа к данным. Также реализовывал "
        "интеграции и обработку ошибок.\n\nБуду рад подробнее рассказать о выполненных "
        "проектах и обсудить задачи команды на собеседовании."
    )

    validate_cover_letter(text, _vacancy(), _fact())
