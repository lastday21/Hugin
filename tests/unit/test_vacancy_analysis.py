from __future__ import annotations

from decimal import Decimal

import pytest

from hugin.domain.directions import SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.services.vacancy_analysis import (
    PythonBackendRules,
    RuleCategory,
    RuleContext,
)


@pytest.mark.parametrize(
    ("vacancy", "reason"),
    [
        (
            VacancyData(
                "2",
                "Продуктовый аналитик",
                "https://hh.ru/vacancy/2",
                description="Используем Python и SQL",
            ),
            "другое направление: аналитика",
        ),
        (
            VacancyData(
                "3",
                "Backend-разработчик",
                "https://hh.ru/vacancy/3",
                description="Разработка на Go",
            ),
            "Python не указан",
        ),
    ],
)
def test_rules_reject_irrelevant_vacancies(vacancy: VacancyData, reason: str) -> None:
    result = PythonBackendRules().evaluate(vacancy)

    assert not result.accepted
    assert result.category is RuleCategory.REJECTED
    assert any(reason in item for item in result.reasons)


def test_rules_accept_junior_python_backend_with_explanation() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "4",
            "Python backend разработчик",
            "https://hh.ru/vacancy/4",
            description="Разработка службы на FastAPI и PostgreSQL",
            experience="Опыт 1\N{EN DASH}3 года",
            work_format="Формат работы: удалённо",
            key_skills=("Python", "FastAPI", "PostgreSQL", "Docker"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.MATCH
    assert result.score >= 55
    assert "Python указан в названии" in result.reasons


def test_three_to_six_years_is_not_a_rejection_for_non_senior_role() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "5",
            "Python-разработчик",
            "https://hh.ru/vacancy/5",
            description="Backend на FastAPI и PostgreSQL",
            experience="Опыт 3\N{EN DASH}6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.MATCH
    assert any("пожелание" in reason for reason in result.reasons)


def test_senior_marker_is_a_risk_but_not_a_rejection() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "senior",
            "Senior Python developer",
            "https://hh.ru/vacancy/senior",
            description="Python backend",
            experience="Опыт 3\N{EN DASH}6 лет",
        )
    )

    assert result.accepted
    assert any("без анализа обязанностей" in reason for reason in result.reasons)


def test_low_soft_score_does_not_reject_related_vacancy() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "low-score",
            "Инженер автоматизации",
            "https://hh.ru/vacancy/low-score",
            description="Писать небольшие инструменты на Python",
        )
    )

    assert result.score < PythonBackendRules.soft_boundary
    assert result.accepted
    assert any("только на порядок очереди" in reason for reason in result.reasons)


def test_leading_role_is_not_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "6",
            "Principal Backend Engineer / Ведущий Python-разработчик",
            "https://hh.ru/vacancy/6",
            description="Backend на FastAPI и PostgreSQL",
            experience="Опыт 3\N{EN DASH}6 лет",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.MATCH


def test_python_automation_with_llm_is_a_match() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "7",
            "Разработчик / Automation Engineer (интеграции, LLM/RAG)",
            "https://hh.ru/vacancy/7",
            description="Писать backend-сервисы на Python и работать через API моделей",
            experience="Опыт 1\N{EN DASH}3 года",
            key_skills=("Python", "FastAPI", "Docker", "Git"),
        )
    )

    assert result.category is RuleCategory.MATCH


def test_ai_agent_engineer_is_a_stretch_match() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "8",
            "AI Agent Engineer (NLP/LLM)",
            "https://hh.ru/vacancy/8",
            description="Логика агентов на Python и интеграция через backend API",
            experience="Опыт 1\N{EN DASH}3 года",
            key_skills=("Python", "LangGraph", "LLM", "NLP"),
        )
    )

    assert result.accepted
    assert result.category is RuleCategory.STRETCH
    assert any("дополнительная подготовка" in reason for reason in result.reasons)


def test_rule_components_use_known_settings_without_zero_for_unknown_values() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "settings",
            "Python backend разработчик",
            "https://hh.ru/vacancy/settings",
            description="Разработка API на FastAPI и PostgreSQL.",
            work_format="Удалённо",
            region="Москва",
            salary_from=Decimal("140000"),
            salary_currency="RUR",
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        ),
        RuleContext(
            skills=("Python, FastAPI, PostgreSQL, Docker",),
            work_formats=(WorkFormat.REMOTE,),
            regions=(SearchRegion("1", "Москва"),),
            desired_salary=120000,
        ),
    )

    assert result.accepted
    assert {component.name for component in result.components} >= {
        "role",
        "skills",
        "format",
        "salary",
        "region",
        "description",
    }
    assert all(component.score > 0 for component in result.components)


@pytest.mark.parametrize(
    ("vacancy", "reason"),
    [
        (
            VacancyData(
                "closed",
                "Python разработчик",
                "https://hh.ru/vacancy/closed",
                description="Python",
                availability=VacancyAvailability.ARCHIVED,
            ),
            "недоступна",
        ),
        (
            VacancyData(
                "scam",
                "Python разработчик",
                "https://hh.ru/vacancy/scam",
                description="Для начала нужно оплатить обучение и прислать код из СМС.",
            ),
            "подозрительное требование",
        ),
    ],
)
def test_closed_and_suspicious_vacancies_are_rejected(
    vacancy: VacancyData,
    reason: str,
) -> None:
    result = PythonBackendRules().evaluate(vacancy)

    assert result.category is RuleCategory.REJECTED
    assert any(reason in item for item in result.reasons)


def test_mandatory_work_format_conflict_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "office",
            "Python разработчик",
            "https://hh.ru/vacancy/office",
            description="Python backend",
            work_format="Только офис",
        ),
        RuleContext(work_formats=(WorkFormat.REMOTE,)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("формат работы" in reason for reason in result.reasons)


def test_mandatory_relocation_outside_selected_cities_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation",
            description=(
                "Python backend. Обязательным условием является релокация "
                "в Республику Татарстан, город Елабуга."
            ),
        ),
        RuleContext(regions=(SearchRegion("1", "Москва"), SearchRegion("2", "Санкт-Петербург"))),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)
