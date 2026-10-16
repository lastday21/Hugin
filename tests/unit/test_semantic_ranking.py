from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.domain.vacancy_priority import FitTier
from hugin.services.semantic_ranking import semantic_evaluation
from hugin.services.semantic_selection import SemanticDecision
from hugin.services.vacancy_analysis import PythonBackendRules, RuleCategory, RuleContext


def vacancy() -> VacancyData:
    return VacancyData(
        "1",
        "Специалист",
        "https://hh.ru/vacancy/1",
        description="Создавать сервисы и отчёты",
        work_format="Удалённо",
    )


@pytest.mark.parametrize(
    "title",
    [
        "Специалист",
        "Junior Python под руководством Senior разработчика",
        "Senior Python Backend Developer (MWS AI)",
        "Team leader по внедрению ИИ на производство",
    ],
)
def test_semantic_profession_replaces_keyword_requirement(title: str) -> None:
    result = semantic_evaluation(
        replace(
            vacancy(),
            title=title,
            description=(
                "Компания — лидер рынка. Разрабатываем ведущие решения. Создавать сервисы и отчёты"
            ),
        ),
        RuleContext(),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.DIRECT, ("Подтверждено",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.category is RuleCategory.MATCH
    assert result.fit is not None and result.fit.tier == FitTier.DIRECT


@pytest.mark.parametrize(
    "change",
    [
        {"availability": VacancyAvailability.CLOSED},
        {"published_at": datetime.now(UTC) - timedelta(days=31)},
        {"description": "Работа без оплаты"},
        {"responsibilities": "Руководить командой разработки и отвечать за найм."},
        {"required_qualifications": "Основной обязательный стек: Java и Spring."},
        {"responsibilities": "Разрабатывать хранимые процедуры на Oracle PL/SQL."},
        {"responsibilities": "Не рассматриваем Python-разработчиков."},
        {"description": "Обязательный переезд во Владивосток."},
        {"work_format": "На месте работодателя", "region": "Томск"},
    ],
)
def test_semantic_allow_cannot_cancel_independent_constraints(change: dict[str, Any]) -> None:
    result = semantic_evaluation(
        replace(vacancy(), **change),
        RuleContext(work_formats=(WorkFormat.REMOTE,), regions=(SearchRegion("1", "Москва"),)),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.DIRECT, ("Да",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.category is RuleCategory.REJECTED
    assert result.score == 0


@pytest.mark.parametrize(
    ("description", "requirements"),
    [
        ("Разрабатывать Python API. Компания также использует Java.", "Python, FastAPI."),
        ("Компания также использует Java. Разрабатывать Python API.", None),
        ("Разрабатывать Python API.", "Опыт с Python или Java."),
        ("Разрабатывать Python API.", "Python. Желательно знание Java."),
        ("Разрабатывать Python API.", "Python. Понимание чужого кода на Java."),
        ("Работа под руководством Team Lead. Разрабатывать Python API.", "Python."),
        ("Разрабатывать Python API. Без управления командой.", "Python."),
        ("Разрабатывать Python API. Будет плюсом: опыт управления командой.", None),
    ],
)
def test_shared_constraints_keep_secondary_context_and_negated_management(
    description: str, requirements: str | None
) -> None:
    item = replace(
        vacancy(),
        title="Senior Python Backend Developer",
        description=description,
        required_qualifications=requirements,
    )
    context = RuleContext(skills=("Python, FastAPI, PostgreSQL",))
    ordinary = PythonBackendRules().evaluate(item, context)
    semantic = semantic_evaluation(
        item,
        context,
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.DIRECT, ("Python подтверждён",)),
        DirectionScope.PYTHON_BACKEND,
    )

    assert ordinary.accepted
    assert semantic.accepted


def test_semantic_primary_duty_uses_confirmed_specialist_skill() -> None:
    result = semantic_evaluation(
        replace(vacancy(), responsibilities="Разрабатывать хранимые процедуры на PL/SQL."),
        RuleContext(skills=("Python, PostgreSQL, PL/SQL",)),
        DirectionScope.IT_ADJACENT,
        SemanticDecision("ALLOW", FitTier.RELATED, ("PL/SQL подтверждён",)),
        DirectionScope.IT_ADJACENT,
    )

    assert result.accepted


@pytest.mark.parametrize(
    "tier,lower,upper",
    [(FitTier.DIRECT, 70, 100), (FitTier.RELATED, 40, 69), (FitTier.POSSIBLE, 0, 39)],
)
def test_salary_and_experience_reduce_priority_without_rejecting(
    tier: FitTier, lower: int, upper: int
) -> None:
    result = semantic_evaluation(
        replace(vacancy(), salary_from=Decimal(60000), experience="От 3 до 6 лет"),
        RuleContext(desired_salary=120000),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", tier, ("Степень соответствия определена по задачам",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.accepted
    assert next(item.score for item in result.components if item.name == "salary") == 50
    assert result.fit is not None and result.fit.tier == tier
    assert lower <= result.score <= upper
    better_conditions = semantic_evaluation(
        replace(vacancy(), salary_from=Decimal(180000), experience="Нет опыта"),
        RuleContext(desired_salary=120000),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", tier, ("Та же степень соответствия",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.score < better_conditions.score <= upper


@pytest.mark.parametrize(
    "description,accepted,region_score",
    [
        (
            "Тестировать программы. Рассматриваем кандидатов из локаций: "
            "Тверь, Ростов-на-Дону, Краснодар, Санкт-Петербург.",
            True,
            100,
        ),
        (
            "Тестировать программы. Офисы компании: Тверь, Санкт-Петербург.",
            False,
            20,
        ),
        (
            "Тестировать программы. Не рассматриваем кандидатов из локаций: Санкт-Петербург.",
            False,
            20,
        ),
        (
            "Тестировать программы. Рассматриваем кандидатов из локаций: "
            "Тверь, Санкт-Петербург. Полный рабочий день в офисе в Твери.",
            False,
            20,
        ),
    ],
)
def test_explicit_candidate_locations_can_differ_from_card_region(
    description: str, accepted: bool, region_score: int
) -> None:
    result = semantic_evaluation(
        replace(vacancy(), description=description, region="Тверь", work_format="Офис или гибрид"),
        RuleContext(
            regions=(SearchRegion("2", "Санкт-Петербург"),),
            work_formats=(WorkFormat.ON_SITE, WorkFormat.HYBRID),
        ),
        DirectionScope.IT_ADJACENT,
        SemanticDecision("ALLOW", FitTier.RELATED, ("Тестирование подтверждено",)),
        DirectionScope.IT_ADJACENT,
    )
    assert result.accepted is accepted
    assert next(item.score for item in result.components if item.name == "region") == region_score


def test_pending_and_uncertain_results_do_not_allow_preparation() -> None:
    result = semantic_evaluation(
        vacancy(),
        RuleContext(),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("REVIEW", None, ("Ожидает разбора",)),
        None,
    )
    assert result.category is RuleCategory.REVIEW
    assert not result.accepted
    assert result.score == 0


@pytest.mark.parametrize(
    "description,accepted",
    [
        ("Стажировка: обучение не оплачиваемое. Зарплата после трудоустройства.", False),
        ("Стажировка: обучение неоплачиваемое. После обучения возможен приём в штат.", False),
        ("Оплачиваемая стажировка. Бесплатное обучение и наставничество.", True),
        (
            "Оплачиваемая стажировка, зарплата 60000 рублей с первого дня. "
            "Дополнительное внешнее обучение не оплачиваем.",
            True,
        ),
        (
            "Оплачиваемая стажировка. Дополнительное внешнее обучение неоплачиваемое.",
            True,
        ),
        (
            "Оплачиваемая стажировка. Дополнительное внешнее обучение неоплачиваемое. "
            "После обучения возможен пересмотр зарплаты.",
            True,
        ),
    ],
)
@pytest.mark.parametrize("semantic", [False, True])
def test_unpaid_training_is_not_confused_with_free_training_at_paid_work(
    description: str, accepted: bool, semantic: bool
) -> None:
    item = replace(
        vacancy(),
        title="Стажёр Python backend разработчик",
        description="Разработка backend на Python. " + description,
    )
    result = (
        semantic_evaluation(
            item,
            RuleContext(),
            DirectionScope.PYTHON_BACKEND,
            SemanticDecision("ALLOW", FitTier.DIRECT, ("Python подтверждён",)),
            DirectionScope.PYTHON_BACKEND,
        )
        if semantic
        else PythonBackendRules().evaluate(item)
    )
    assert result.accepted is accepted
