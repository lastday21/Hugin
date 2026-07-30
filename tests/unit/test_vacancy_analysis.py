from __future__ import annotations

from decimal import Decimal

import pytest

from hugin.domain.directions import DirectionScope, SearchRegion, WorkFormat
from hugin.domain.vacancies import VacancyAvailability, VacancyData
from hugin.services.vacancy_analysis import (
    AdjacentItRules,
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


def test_automation_is_routed_from_python_backend_to_it() -> None:
    vacancy = VacancyData(
        "low-score",
        "Инженер автоматизации",
        "https://hh.ru/vacancy/low-score",
        description="Писать небольшие инструменты на Python",
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert routed.target_scope is DirectionScope.IT_ADJACENT
    assert result.accepted


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


@pytest.mark.parametrize(
    "title",
    [
        "Архитектор-разработчик Python",
        "Tech Lead / Backend Architect (Python)",
        "Tech Lead / Руководитель команды разработки Python",
    ],
)
def test_hands_on_python_leadership_title_is_not_a_hard_rejection(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"lead-{title}",
            title,
            "https://hh.ru/vacancy/lead",
            description=(
                "Разрабатывать backend на Python, писать модули и тесты, "
                "реализовывать микросервисы и интеграции."
            ),
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        )
    )

    assert result.category is RuleCategory.MATCH


def test_python_test_automation_in_responsibilities_is_routed_to_it() -> None:
    vacancy = VacancyData(
        "python-load-tests",
        "Ведущий инженер по тестированию",
        "https://hh.ru/vacancy/python-load-tests",
        responsibilities=("Разрабатывать сценарии нагрузочного тестирования на Locust и Python."),
        key_skills=("Python", "Locust"),
    )

    routed = PythonBackendRules().evaluate(vacancy)
    accepted = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert routed.target_scope is DirectionScope.IT_ADJACENT
    assert accepted.category is RuleCategory.MATCH


def test_python_automation_with_llm_is_a_match() -> None:
    vacancy = VacancyData(
        "7",
        "Разработчик / Automation Engineer (интеграции, LLM/RAG)",
        "https://hh.ru/vacancy/7",
        description="Писать backend-сервисы на Python и работать через API моделей",
        experience="Опыт 1\N{EN DASH}3 года",
        key_skills=("Python", "FastAPI", "Docker", "Git"),
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert result.category is RuleCategory.MATCH


def test_ai_agent_engineer_is_a_stretch_match() -> None:
    vacancy = VacancyData(
        "8",
        "AI Agent Engineer (NLP/LLM)",
        "https://hh.ru/vacancy/8",
        description="Логика агентов на Python и интеграция через backend API",
        experience="Опыт 1\N{EN DASH}3 года",
        key_skills=("Python", "LangGraph", "LLM", "NLP"),
    )
    routed = PythonBackendRules().evaluate(vacancy)
    result = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
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


def test_mandatory_relocation_is_rejected_when_candidate_is_not_ready() -> None:
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
        RuleContext(
            regions=(SearchRegion("1", "Москва"), SearchRegion("2", "Санкт-Петербург")),
            relocation_allowed=False,
        ),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


def test_mandatory_relocation_is_allowed_when_candidate_confirmed_it() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation-confirmed",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation-confirmed",
            description="Python backend. Обязательная релокация в город Казань.",
        ),
        RuleContext(relocation_allowed=True),
    )

    assert result.category is RuleCategory.MATCH


def test_mandatory_far_east_relocation_is_rejected_even_when_relocation_allowed() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "relocation-far-east-confirmed",
            "Python разработчик",
            "https://hh.ru/vacancy/relocation-far-east-confirmed",
            description="Python backend. Обязательная релокация во Владивосток.",
        ),
        RuleContext(relocation_allowed=True),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


@pytest.mark.parametrize("city", ["Владивосток", "Хабаровск"])
def test_mandatory_relocation_to_far_east_is_rejected(city: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"relocation-{city}",
            "Python backend разработчик",
            f"https://hh.ru/vacancy/relocation-{city}",
            description=f"Python backend. Обязательная релокация в город {city}.",
        )
    )

    assert result.category is RuleCategory.REJECTED
    assert any("переезд" in reason for reason in result.reasons)


def test_fullstack_is_owned_by_it_even_with_python_backend() -> None:
    vacancy = VacancyData(
        "fullstack",
        "Middle Full-stack разработчик",
        "https://hh.ru/vacancy/fullstack",
        description="Python, FastAPI, TypeScript и React",
    )

    routed = PythonBackendRules().evaluate(vacancy)
    accepted = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert routed.target_scope is DirectionScope.IT_ADJACENT
    assert accepted.category is RuleCategory.MATCH


def test_machine_learning_role_is_a_stretch_in_it() -> None:
    vacancy = VacancyData(
        "ml",
        "Middle ML-инженер",
        "https://hh.ru/vacancy/ml",
        description="Разработка служб на Python и обучение моделей",
    )

    routed = PythonBackendRules().evaluate(vacancy)
    accepted = AdjacentItRules().evaluate(vacancy)

    assert routed.category is RuleCategory.ROUTED
    assert accepted.category is RuleCategory.STRETCH


def test_unrelated_mandatory_dotnet_stack_is_rejected_in_it() -> None:
    result = AdjacentItRules().evaluate(
        VacancyData(
            "dotnet",
            "Backend C# Developer",
            "https://hh.ru/vacancy/dotnet",
            required_qualifications="Обязательный стек: C# и .NET",
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("другой основной стек" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Java Developer",
        "Backend C# Developer",
        "Golang разработчик",
        "Node.js Backend Developer / NestJS",
        "Embedded C++ разработчик",
    ],
)
def test_other_primary_stack_is_rejected_even_with_generic_skill_overlap(title: str) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"other-{title}",
            title,
            "https://hh.ru/vacancy/other-stack",
            description="Backend, REST API, PostgreSQL, Docker и Linux.",
            required_qualifications="Обязателен основной стек из названия.",
            key_skills=("PostgreSQL", "Docker", "Linux"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker, Linux",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("другой основной стек" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("title", "description", "stack"),
    [
        (
            "Стажер-backend developer",
            (
                "Основной стек — NodeJS, TypeScript, NestJS и Rust. "
                "Python указан лишь как пример базового языка."
            ),
            "Node.js/TypeScript",
        ),
        (
            "Senior Backend (Rust/C++/Python)",
            ("Основной язык — Rust; Python используется для legacy и вспомогательных сервисов."),
            "Rust",
        ),
    ],
)
def test_python_as_secondary_language_does_not_hide_primary_stack(
    title: str,
    description: str,
    stack: str,
) -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            f"secondary-{stack}",
            title,
            "https://hh.ru/vacancy/secondary",
            description=description,
            key_skills=("Python", "Docker"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL, Docker",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any(stack in reason for reason in result.reasons)


def test_not_only_python_phrase_is_not_treated_as_secondary_language() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "python-not-only",
            "Python-разработчик",
            "https://hh.ru/vacancy/python-not-only",
            description=(
                "Ищем Python-разработчика, который умеет не только писать код, "
                "но и проектировать backend-сервисы на FastAPI."
            ),
            key_skills=("Python", "FastAPI", "PostgreSQL"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.MATCH


def test_mandatory_primary_1c_stack_is_rejected() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "primary-1c",
            "Архитектор ERP системы",
            "https://hh.ru/vacancy/primary-1c",
            description=(
                "Глубокая разработка в 1С (основной стек). "
                "Python применяется только для внешних расчётных модулей."
            ),
            key_skills=("1С: Предприятие 8", "Python"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.REJECTED
    assert any("1С" in reason for reason in result.reasons)


def test_mixed_go_and_python_backend_title_is_not_rejected_as_other_stack() -> None:
    result = PythonBackendRules().evaluate(
        VacancyData(
            "mixed-stack",
            "Backend Go / Python разработчик",
            "https://hh.ru/vacancy/mixed-stack",
            description="Разработка сервисов на Python и Go.",
            key_skills=("Python", "Go", "PostgreSQL"),
        ),
        RuleContext(skills=("Python, FastAPI, PostgreSQL",)),
    )

    assert result.category is RuleCategory.MATCH
