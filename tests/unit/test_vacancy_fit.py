from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from itertools import permutations

import pytest

from hugin.domain.vacancies import VacancyData
from hugin.domain.vacancy_priority import FitTier, stored_fit_tier, vacancy_priority_key
from hugin.services.vacancy_analysis import (
    AdjacentItRules,
    PythonBackendRules,
    RuleCategory,
    RuleContext,
)

PROFILE = RuleContext(
    skills=("Python", "FastAPI", "PostgreSQL", "YandexGPT", "Docker", "pytest"),
    desired_salary=120_000,
)


def vacancy(title: str, duties: str, requirements: str = "Python и REST API") -> VacancyData:
    return VacancyData(
        "fit-case",
        title,
        "https://hh.ru/vacancy/fit-case",
        responsibilities=duties,
        required_qualifications=requirements,
        experience="От 1 года до 3 лет",
    )


@pytest.mark.parametrize(
    ("rules", "data", "tier"),
    [
        (
            PythonBackendRules(),
            vacancy("Python backend разработчик", "Разработка API на Python и FastAPI"),
            FitTier.DIRECT,
        ),
        (
            AdjacentItRules(),
            vacancy("Разработчик LLM", "Разрабатывать сервисы на Python с LLM API"),
            FitTier.DIRECT,
        ),
        (
            AdjacentItRules(),
            vacancy("Разработчик автоматизации", "Разрабатывать интеграции на Python и REST API"),
            FitTier.DIRECT,
        ),
        (
            AdjacentItRules(),
            vacancy(
                "Инженер автоматизации тестирования Python",
                "Разрабатывать автотесты Python, pytest и REST API",
            ),
            FitTier.RELATED,
        ),
        (
            AdjacentItRules(),
            vacancy("DevOps инженер", "Автоматизировать эксплуатацию Linux и Docker на Python"),
            FitTier.POSSIBLE,
        ),
        (
            AdjacentItRules(),
            vacancy(
                "Специалист технической поддержки",
                "Поддержка программ, настройка Python и REST API",
            ),
            FitTier.POSSIBLE,
        ),
        (
            PythonBackendRules(),
            vacancy("Senior Python backend разработчик", "Разрабатывать API на Python"),
            FitTier.RELATED,
        ),
        (
            PythonBackendRules(),
            vacancy(
                "Python backend разработчик",
                "Разрабатывать API на Python",
                "Python, FastAPI, опыт от 5 лет",
            ),
            FitTier.POSSIBLE,
        ),
        (
            PythonBackendRules(),
            vacancy(
                "Python backend разработчик",
                "Разрабатывать API на Python",
                "Python, FastAPI, опыт от 3 лет",
            ),
            FitTier.RELATED,
        ),
        (
            PythonBackendRules(),
            vacancy(
                "Python backend разработчик",
                "Разрабатывать API на Python",
                "Обязательно знание Kafka и Kubernetes",
            ),
            FitTier.POSSIBLE,
        ),
    ],
)
def test_fit_tiers_follow_tasks_and_confirmed_skills(
    rules: PythonBackendRules, data: VacancyData, tier: FitTier
) -> None:
    result = rules.evaluate(data, PROFILE)
    assert result.accepted, result.reasons
    assert result.fit is not None
    assert result.fit.tier is tier, result.fit


def test_applied_ai_does_not_need_an_adjacent_penalty_when_work_is_confirmed() -> None:
    result = AdjacentItRules().evaluate(
        vacancy("AI developer", "Разрабатывать Python сервисы и интеграции LLM API"), PROFILE
    )
    assert result.category is RuleCategory.MATCH
    assert result.fit is not None and result.fit.tier is FitTier.DIRECT
    assert not any("отдельная специализация" in reason for reason in result.reasons)


def test_optional_and_company_stack_does_not_prove_direct_fit() -> None:
    data = vacancy("Python backend разработчик", "Разработка продукта")
    data = replace(
        data,
        required_qualifications="Python",
        preferred_qualifications="FastAPI, PostgreSQL, Docker, LLM API",
        description="Будет плюсом: FastAPI. Мы предлагаем: работу с LLM API и PostgreSQL",
    )
    result = PythonBackendRules().evaluate(data, PROFILE)
    assert result.fit is not None
    assert result.fit.tier is FitTier.RELATED
    assert result.fit.matched_capabilities == ("python",)


def test_missing_profile_does_not_look_like_ideal_match() -> None:
    result = PythonBackendRules().evaluate(
        vacancy("Python backend разработчик", "Разработка Python API"), RuleContext()
    )
    assert result.fit is not None and result.fit.tier is FitTier.POSSIBLE


def test_salary_affects_score_inside_tier_and_cannot_override_profession() -> None:
    data = vacancy("Python backend разработчик", "Разработка API на Python и FastAPI")
    low = PythonBackendRules().evaluate(
        replace(data, salary_from=Decimal(60_000), salary_currency="RUR", salary_gross=False),
        PROFILE,
    )
    high = PythonBackendRules().evaluate(
        replace(data, salary_from=Decimal(150_000), salary_currency="RUR", salary_gross=False),
        PROFILE,
    )
    assert low.accepted and high.accepted
    assert low.fit is not None and high.fit is not None
    assert low.fit.tier is high.fit.tier is FitTier.DIRECT
    assert high.score > low.score
    assert vacancy_priority_key({"fit_tier": 1}, low.score, 2) < vacancy_priority_key(
        {"fit_tier": 3}, 100, 1
    )


def test_foreign_required_profession_still_rejected_with_high_salary() -> None:
    data = vacancy(
        "Python backend разработчик", "Разрабатывать API", "Обязателен опыт Java и Spring"
    )
    result = PythonBackendRules().evaluate(
        replace(data, salary_from=Decimal(300_000), salary_currency="RUR"), PROFILE
    )
    assert result.category is RuleCategory.REJECTED
    assert result.fit is None


@pytest.mark.parametrize("title", ["Специалист технической поддержки", "Technical support"])
def test_support_outside_it_is_not_accepted_for_general_customer_work(title: str) -> None:
    data = vacancy(
        title,
        "Консультировать покупателей бытовой техники по телефону",
        "Грамотная речь, опыт обслуживания покупателей",
    )
    data = replace(data, preferred_qualifications="Будет плюсом: Python и SQL")
    result = AdjacentItRules().evaluate(data, PROFILE)
    assert result.category is RuleCategory.REJECTED
    assert result.fit is None


def test_named_development_department_does_not_hide_required_administration() -> None:
    data = vacancy(
        "Дежурный Linux-инженер в отдел разработки решений и автоматизации",
        "Приём звонков, консультации пользователей, разработка инструкций",
        "Опыт администрирования Linux, Active Directory, FreeIPA, dovecot и postfix. "
        "Разработка скриптов Python, работа с PostgreSQL.",
    )
    result = AdjacentItRules().evaluate(data, PROFILE)
    assert result.category is RuleCategory.REJECTED
    assert any("специализации администрирования" in reason for reason in result.reasons)
    optional = replace(
        data,
        required_qualifications="Python, PostgreSQL. Будет плюсом: Active Directory и dovecot",
    )
    result = AdjacentItRules().evaluate(optional, PROFILE)
    assert result.accepted
    assert result.fit is not None and result.fit.tier is FitTier.POSSIBLE


def test_structured_duties_do_not_hide_required_skills_in_description() -> None:
    data = vacancy("Python backend разработчик", "Разрабатывать внутренние сервисы")
    data = replace(
        data,
        required_qualifications=None,
        description="Будет плюсом:\nRedis.\nКандидат нам подходит если:\nPython и FastAPI.",
    )
    result = PythonBackendRules().evaluate(data, PROFILE)
    assert result.fit is not None and result.fit.tier is FitTier.DIRECT


def test_mixed_middle_senior_accepts_current_level_when_experience_matches() -> None:
    result = PythonBackendRules().evaluate(
        vacancy("Middle/Senior Python backend разработчик", "Разрабатывать Python API"), PROFILE
    )
    assert result.fit is not None and result.fit.tier is FitTier.DIRECT


@pytest.mark.parametrize("raw", [None, "1", True, 0, 4, [], {}])
def test_unknown_stored_tier_is_not_invented(raw: object) -> None:
    assert stored_fit_tier({"fit_tier": raw}) is None


def test_zero_score_and_components_remain_zero_in_priority() -> None:
    assert vacancy_priority_key(
        {"fit_tier": 1, "location_priority": 0, "experience_priority": 0}, 0, 9
    ) == (1, 0, 0, 0, 9)


SPECIALIZED_OPERATIONS = (
    (
        "DevOps-инженер",
        "Уверенные знания и опыт использования Helm / Kubernetes, систем виртуализации.",
        ("Kubernetes",),
        "инфраструктуры Kubernetes",
    ),
    (
        "DevOps-инженер",
        "Production-опыт с Kubernetes: несколько кластеров, networking, ingress, autoscaling.",
        ("Kubernetes",),
        "инфраструктуры Kubernetes",
    ),
    (
        "SRE инженер",
        "Опыт развёртывания и эксплуатации LLM-моделей на GPU: vLLM или Triton.\n"
        "Управление ресурсами GPU в K8s: HAMi, MIG, квотирование.",
        ("vLLM", "Kubernetes", "GPU"),
        "GPU",
    ),
    (
        "Инженер по информационной безопасности",
        "Практические навыки работы с СКЗИ: КриптоПро CSP и КриптоПро HSM.",
        ("КриптоПро CSP",),
        "криптографической защиты",
    ),
)


@pytest.mark.parametrize(("title", "required", "confirmed", "reason"), SPECIALIZED_OPERATIONS)
def test_operating_specialization_requires_confirmed_tools(
    title: str, required: str, confirmed: tuple[str, ...], reason: str
) -> None:
    data = vacancy(title, "Автоматизировать серверные процессы на Python.", required)
    result = AdjacentItRules().evaluate(data, PROFILE)
    assert not result.accepted
    assert any(reason in item for item in result.reasons)
    supported = replace(PROFILE, skills=(*PROFILE.skills, *confirmed))
    assert AdjacentItRules().evaluate(data, supported).accepted


@pytest.mark.parametrize(("title", "required", "confirmed", "reason"), SPECIALIZED_OPERATIONS)
@pytest.mark.parametrize("optional_heading", ["Будет плюсом", "Плюсом будет", "Желательно"])
def test_optional_operating_specialization_never_becomes_a_requirement(
    title: str,
    required: str,
    confirmed: tuple[str, ...],
    reason: str,
    optional_heading: str,
) -> None:
    sections = (
        "Требования:\nPython и SQL.",
        f"{optional_heading}:\n{required}",
        "Мы предлагаем:\nПрактический опыт эксплуатации Kubernetes, GPU и КриптоПро.",
    )
    for ordered in permutations(sections):
        data = vacancy(title, "Автоматизировать серверные процессы на Python.")
        data = replace(data, description="\n".join(ordered), required_qualifications=required)
        result = AdjacentItRules().evaluate(data, PROFILE)
        assert result.accepted, result.reasons
        assert not any("специализации администрирования" in item for item in result.reasons)


@pytest.mark.parametrize(("title", "required", "confirmed", "reason"), SPECIALIZED_OPERATIONS)
def test_mandatory_operating_specialization_survives_optional_section_reordering(
    title: str, required: str, confirmed: tuple[str, ...], reason: str
) -> None:
    sections = (
        f"Требования:\n{required}",
        "Плюсом будет:\nPython и SQL.",
        "Мы предлагаем:\nОбучение коллегами.",
    )
    for ordered in permutations(sections):
        data = vacancy(title, "Автоматизировать серверные процессы на Python.")
        result = AdjacentItRules().evaluate(replace(data, description="\n".join(ordered)), PROFILE)
        assert not result.accepted
        assert any(reason in item for item in result.reasons)


@pytest.mark.parametrize(
    "requirements",
    [
        "Знакомство с Kubernetes и Helm на уровне разработчика.",
        "Базовое понимание Kubernetes: ingress, autoscaling.",
        "Практические навыки работы с Docker. Знакомство с Kubernetes и Helm.",
        "Работа с готовым API LLM-сервиса, который запускается на GPU в Kubernetes.",
    ],
)
def test_basic_cluster_awareness_and_service_usage_are_not_production_operations(
    requirements: str,
) -> None:
    data = vacancy("DevOps-инженер", "Автоматизировать задачи на Python.", requirements)
    assert AdjacentItRules().evaluate(data, PROFILE).accepted


def test_application_developer_is_not_an_infrastructure_operator() -> None:
    data = vacancy(
        "Python backend разработчик",
        "Разрабатывать API на Python и FastAPI.",
        "Python, FastAPI. Опыт использования Helm / Kubernetes на уровне разработчика.",
    )
    assert PythonBackendRules().evaluate(data, PROFILE).accepted


def test_theoretical_crypto_knowledge_is_not_practical_cryptographic_administration() -> None:
    data = vacancy(
        "Инженер по информационной безопасности",
        "Автоматизировать отчёты на Python.",
        "Python и SQL. Базовое понимание назначения СКЗИ и КриптоПро.",
    )
    assert AdjacentItRules().evaluate(data, PROFILE).accepted


@pytest.mark.parametrize(
    ("requirements", "confirmed"),
    [
        ("Опыт эксплуатации моделей на GPU: vLLM или Triton.", ("Triton",)),
        ("Управление ресурсами GPU в K8s: MIG и квотирование.", ("Kubernetes", "GPU")),
    ],
)
def test_gpu_serving_and_cluster_resource_management_have_separate_evidence(
    requirements: str, confirmed: tuple[str, ...]
) -> None:
    data = vacancy("SRE инженер", "Поддерживать серверные приложения.", requirements)
    context = replace(PROFILE, skills=(*PROFILE.skills, *confirmed))
    assert AdjacentItRules().evaluate(data, context).accepted


@pytest.mark.parametrize(
    "title",
    [
        "Site Reliability Engineer",
        "DevSecOps инженер",
        "Platform Engineer",
        "System Administrator",
    ],
)
def test_english_operations_titles_keep_the_same_mandatory_boundary(title: str) -> None:
    data = vacancy(
        title,
        "Автоматизировать инфраструктуру на Python.",
        "Production‑опыт с Kubernetes: несколько кластеров, ingress, autoscaling.",
    )
    result = AdjacentItRules().evaluate(data, PROFILE)
    assert not result.accepted
    assert any("инфраструктуры Kubernetes" in item for item in result.reasons)
