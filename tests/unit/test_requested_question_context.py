# ruff: noqa: RUF001

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    ApplicationModel,
    CandidateProfileModel,
    CoverLetterFactModel,
    CoverLetterModel,
    DirectionVacancyModel,
    ResumeModel,
    VacancyModel,
    VerifiedFactModel,
)
from hugin.domain.content import ConfirmationState
from hugin.services import cover_letter as letter_module
from hugin.services.cover_letter import CoverLetterService
from tests.unit.test_cover_letter import FakeModel, _letter, _prepare_data


@pytest.mark.parametrize(
    "case",
    [
        "no_question",
        "question",
        "tight_budget",
        "second_question",
        "covered_words",
        "dotted_technology",
        "verb_overlap",
    ],
)
def test_direct_question_keeps_its_confirmed_answer_after_primary_facts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    direct_question = case != "no_question"
    tight_budget = case == "tight_budget"
    dotted_technology = case in {"dotted_technology", "verb_overlap"}
    detail = "Node.js" if dotted_technology else "Codex"
    tools_answer = (
        "Для разработки использую Node.js, проверяю изменения кода тестами."
        if dotted_technology
        else "AI-инструменты для разработки: использую Codex, локальные модели в Ollama. "
        "Проверяю изменения кода и результат тестами."
    )
    database = create_database(settings)
    model = FakeModel(
        [
            _letter()
            + (
                "\n\nВ отдельном проекте разработал интерфейс на React и TypeScript, "
                "настроил браузерные проверки на Playwright."
                if not tight_budget
                else ""
            )
            + ("\n\n" + tools_answer if direct_question else "")
        ]
        * 8
    )
    try:
        with database.sessions.begin() as session:
            account_id, direction_id, resume_id, vacancy_ids = _prepare_data(session)
            profile = session.scalar(
                select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
            )
            vacancy = session.get(VacancyModel, vacancy_ids[0])
            assert profile is not None and vacancy is not None
            vacancy.description = (
                "Разрабатывать серверные приложения на Python с FastAPI, PostgreSQL и Redis. "
                "Проектировать REST API, интеграции и обработку данных, развивать автоматизацию. "
                "Развивать интерфейс на React и TypeScript, браузерные проверки на Playwright. "
                "В проекте используются AI-инструменты и платформы для разработки."
                + (
                    "\n"
                    + (
                        "Какой у вас опыт работы с PostgreSQL? "
                        if case == "second_question"
                        else ""
                    )
                    + (
                        "Вы используете Node.js?"
                        if dotted_technology
                        else "Какие AI-инструменты и платформы для разработки "
                        "вы используете на практике?"
                    )
                    if direct_question
                    else ""
                )
            )
            project = VerifiedFactModel(
                profile_id=profile.id,
                category="project",
                content=(
                    "В отдельном проекте разработал интерфейс на React и TypeScript. "
                    "Настроил браузерные проверки на Playwright."
                ),
                source_type="project",
                resume_id=resume_id,
                state=ConfirmationState.CONFIRMED,
                allow_in_letters=True,
            )
            if case == "covered_words":
                project.content += (
                    " Использую AI-инструменты и платформы для разработки на практике."
                )
            answer = VerifiedFactModel(
                profile_id=profile.id,
                category="about",
                content=(tools_answer + "\nTelegram: @private_contact\nprivate@example.com"),
                source_type="user",
                resume_id=resume_id,
                state=ConfirmationState.CONFIRMED,
                allow_in_letters=True,
            )
            session.add_all((project, answer))
            if case == "verb_overlap":
                session.add(
                    VerifiedFactModel(
                        profile_id=profile.id,
                        category="project",
                        content="Использую Blender для моделирования домов.",
                        source_type="user",
                        resume_id=resume_id,
                        state=ConfirmationState.CONFIRMED,
                        allow_in_letters=True,
                    )
                )
            session.flush()
            service = CoverLetterService(session, model)
            candidate = service._candidates(account_id, direction_id)[0]
            if tight_budget:
                primary = tuple(
                    fact
                    for fact in service._select_facts(candidate, direction_id)
                    if fact.id != answer.id
                )
                assert len(primary) == 2
                budget = (
                    sum(len(fact.content) for fact in primary)
                    + len(tools_answer)
                    - len(primary[-1].content) // 2
                )
                monkeypatch.setattr(letter_module, "MAX_FACT_CONTEXT_LENGTH", budget)

            result = service.prepare(
                account_id=account_id,
                direction_name="Python backend",
                vacancy_hh_id=vacancy.hh_id,
                limit=1,
            )

            assert result.generated == 1, result.items
            context = model.prompts[0][1].split("<confirmed_facts>", 1)[1]
            assert "FastAPI" in context
            assert ("React" in context) is (not tight_budget)
            assert (detail in context) is direct_question
            assert "@private_contact" not in context
            assert "private@example.com" not in context
            assert "Blender" not in context
            letter = session.scalar(select(CoverLetterModel))
            assert letter is not None
            linked_ids = set(
                session.scalars(
                    select(CoverLetterFactModel.fact_id).where(
                        CoverLetterFactModel.cover_letter_id == letter.id
                    )
                )
            )
            assert (answer.id in linked_ids) is direct_question
            selected = service._select_facts(candidate, direction_id)
            assert (
                sum(len(fact.content) for fact in selected) <= letter_module.MAX_FACT_CONTEXT_LENGTH
            )
            service.validate_for_submission(
                application_id=letter.application_id, letter_id=letter.id
            )
    finally:
        database.close()


def test_budget_evicts_general_experience_before_another_direct_answer() -> None:
    def padded(prefix: str, length: int) -> str:
        filler = " Выполнял согласованные технические задачи."
        return (prefix + filler * 200)[:length].rsplit(" ", 1)[0]

    facts = [
        VerifiedFactModel(
            id=identifier,
            category=category,
            content=padded(prefix, length),
            source_type="user",
        )
        for identifier, category, prefix, length in (
            (
                1,
                "work_experience",
                "Разрабатывал сервисы на FastAPI PostgreSQL Redis "
                "SQLAlchemy asyncio pytest Celery Docker.",
                4380,
            ),
            (2, "project", "Использую Kubernetes для развёртывания приложений.", 180),
            (3, "about", "Использую Node.js для разработки серверных приложений.", 2490),
        )
    ]
    vacancy = VacancyModel(
        title="Python backend",
        description=(
            "Разработка сервисов FastAPI PostgreSQL Redis SQLAlchemy "
            "asyncio pytest Celery Docker. "
            "Используете Kubernetes? Используете Node.js?"
        ),
        key_skills=[],
    )
    candidate = letter_module._Candidate(
        ApplicationModel(account_id=1), vacancy, ResumeModel(id=1), DirectionVacancyModel()
    )
    session = cast(Session, SimpleNamespace(scalars=lambda _statement: facts))

    selected = CoverLetterService(session)._select_facts(candidate, 1)

    assert {2, 3} <= {fact.id for fact in selected}
    assert sum(len(fact.content) for fact in selected) <= letter_module.MAX_FACT_CONTEXT_LENGTH
