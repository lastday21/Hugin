from __future__ import annotations

import pytest

from hugin.domain.vacancies import VacancyData
from hugin.services.vacancy_analysis import PythonBackendRules, RuleCategory


@pytest.mark.parametrize(
    ("vacancy", "reason"),
    [
        (
            VacancyData(
                "1",
                "Senior Python developer",
                "https://hh.ru/vacancy/1",
                description="Python backend",
                experience="Опыт 3\N{EN DASH}6 лет",
            ),
            "уровень Senior",
        ),
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
