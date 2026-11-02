from __future__ import annotations

import pytest

from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyData
from hugin.domain.vacancy_priority import FitTier
from hugin.services.semantic_ranking import semantic_evaluation
from hugin.services.semantic_selection import SemanticDecision
from hugin.services.vacancy_analysis import RuleContext, RuleEvaluation


def evaluate(description: str, *, title: str = "Python backend разработчик") -> RuleEvaluation:
    return semantic_evaluation(
        VacancyData(
            "review",
            title,
            "https://hh.ru/vacancy/review",
            description=description,
            work_format="Удалённо",
            region="Москва",
        ),
        RuleContext(
            work_formats=(WorkFormat.REMOTE,),
            regions=(SearchRegion("99", "Уфа"),),
        ),
        DirectionScope.PYTHON_BACKEND,
        SemanticDecision("ALLOW", FitTier.RELATED, ("Близкие действия подтверждены",)),
        DirectionScope.PYTHON_BACKEND,
    )


def test_conference_languages_after_optional_stack_are_not_job_requirements() -> None:
    result = evaluate(
        "Требования:\nPython, FastAPI.\nБудет плюсом с :\nGraphQL.\n"
        "Почему компания:\nМы проводим конференции по Go и Python."
    )
    assert result.accepted


@pytest.mark.parametrize(
    "stack",
    [
        "Наши инструменты:\nPython, JavaScript/TypeScript, React и Git. "
        "При необходимости выбираем другие технологии под задачу.\n"
        "Знать весь перечисленный стек не обязательно.",
        "Стек проекта:\nPython; SQL; React + TypeScript; Docker. "
        "Не обязательно быть экспертом во всем стеке.",
    ],
)
def test_explicitly_optional_stack_does_not_override_semantic_transfer(stack: str) -> None:
    assert evaluate("Требования:\nPython, SQL и API.\n" + stack).accepted
    assert not evaluate(
        "Требования:\nPython, SQL и API.\n"
        + stack
        + "\nТребования:\nОбязателен опыт разработки на Go."
    ).accepted


def test_basic_cpp_reading_does_not_mean_cpp_development() -> None:
    assert evaluate(
        "Требования:\nНавыки работы с legacy-кодом (Python + базовое чтение C++), "
        "умение выделять сервисы и покрывать код тестами."
    ).accepted
    assert not evaluate(
        "Требования:\nБазовое чтение C++. Обязательно разрабатывать сервисы на C++."
    ).accepted


def test_explicit_remote_alternative_is_not_required_moscow_office() -> None:
    assert evaluate(
        "Разрабатывать Python API. Работа в офисе в г. Москва или Тольятти "
        "(возможен гибридный и удаленный формат работы)."
    ).accepted
    assert not evaluate(
        "Разрабатывать Python API. Формат работы: только офис в Москве. Удалённо только обучение."
    ).accepted


def test_semantic_technical_leadership_keeps_personnel_management_boundary() -> None:
    description = (
        "Задачи:\nРуководить командой, планировать задачи, проводить ревью. "
        "Определять архитектуру.\nТребования:\nPython, PostgreSQL, Docker."
    )
    assert evaluate(description, title="Lead Python developer").accepted
    assert not evaluate(
        description + "\nОбязанности:\nОтвечать за найм и увольнение сотрудников.",
        title="Lead Python developer",
    ).accepted
