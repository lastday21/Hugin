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
from hugin.services.vacancy_analysis import RuleCategory, RuleContext


def vacancy() -> VacancyData:
    return VacancyData(
        "1",
        "Специалист",
        "https://hh.ru/vacancy/1",
        description="Создавать сервисы и отчёты",
        work_format="Удалённо",
    )


def test_semantic_profession_replaces_keyword_requirement() -> None:
    result = semantic_evaluation(
        vacancy(),
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


def test_salary_and_experience_reduce_priority_without_rejecting() -> None:
    result = semantic_evaluation(
        replace(vacancy(), salary_from=Decimal(60000), experience="От 3 до 6 лет"),
        RuleContext(desired_salary=120000),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.POSSIBLE, ("Нужен больший опыт",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.accepted
    assert next(item.score for item in result.components if item.name == "salary") == 50
    assert result.fit is not None and result.fit.tier == FitTier.POSSIBLE


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
def test_unpaid_training_is_not_confused_with_free_training_at_paid_work(
    description: str, accepted: bool
) -> None:
    result = semantic_evaluation(
        replace(vacancy(), title="Стажёр Python", description=description),
        RuleContext(),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.DIRECT, ("Python подтверждён",)),
        DirectionScope.PYTHON_BACKEND,
    )
    assert result.accepted is accepted
