from __future__ import annotations

from dataclasses import replace

import pytest

from hugin.domain.vacancies import VacancyData
from hugin.services.requirement_sections import primary_requirements
from hugin.services.vacancy_analysis import AdjacentItRules, RuleContext
from hugin.services.vacancy_fit import unsupported_administration


def operating_vacancy(requirement: str, *, security: bool = False) -> VacancyData:
    return VacancyData(
        "operating-evidence",
        "Инженер информационной безопасности" if security else "Инженер DevOps",
        "https://hh.ru/vacancy/operating-evidence",
        description=(
            "Обязанности:\nРазработка автоматизации на Python, FastAPI и SQL.\n"
            "Требования:\n" + requirement
        ),
        required_qualifications=requirement,
    )


def assert_specialization(
    vacancy: VacancyData, *, expected: bool, confirmed: tuple[str, ...] = ()
) -> None:
    skills = ("Python", "FastAPI", "SQL", "Docker", *confirmed)
    required = primary_requirements(vacancy.description, vacancy.required_qualifications)
    gaps = unsupported_administration(vacancy, required, skills)
    assert bool(gaps) is expected, gaps
    result = AdjacentItRules().evaluate(vacancy, RuleContext(skills=skills))
    rejected = any("специализации администрирования" in reason for reason in result.reasons)
    assert rejected is expected, result.reasons
    assert result.accepted is not expected, result.reasons


@pytest.mark.parametrize(
    ("requirement", "security"),
    [
        ("Опыт промышленной эксплуатации Kubernetes не требуется.", False),
        (
            "Практический опыт Python обязателен, "
            "базового знакомства с Kubernetes и Helm достаточно.",
            False,
        ),
        (
            "Kubernetes находится в промышленной эксплуатации у отдельной команды; "
            "кандидату достаточно Python.",
            False,
        ),
        ("Практический опыт Kubernetes и Helm желателен, но не обязателен.", False),
        ("GPU: опыт эксплуатации Triton не нужен.", False),
        ("Навык управления ресурсами GPU с MIG необязателен.", False),
        (
            "GPU и Triton в промышленной эксплуатации у другой команды; "
            "от кандидата требуется Python.",
            False,
        ),
        (
            "Практический опыт SQL обязателен, а CryptoPro достаточно знать на уровне терминов.",
            True,
        ),
        ("Практический опыт работы с CryptoPro не требуется.", True),
        ("Опыт работы с CryptoPro есть у коллег, кандидату достаточно Python.", True),
        ("Базовое понимание принципов управления инфраструктурой через Terraform.", False),
        ("Самостоятельное управление инфраструктурой через Terraform желательно.", False),
        (
            "Terraform используют коллеги; от кандидата требуется разработка Python API.",
            False,
        ),
        (
            "Практический опыт работы с Python. "
            "Базовое понимание назначения Triton и его эксплуатации на GPU.",
            False,
        ),
        (
            "Взаимодействие с командой эксплуатации Kubernetes обязательно; "
            "самостоятельное администрирование не требуется.",
            False,
        ),
        (
            "CryptoPro упомянут в документации. PostgreSQL используется для хранения данных. "
            "Практический опыт его эксплуатации обязателен.",
            True,
        ),
    ],
)
def test_operating_experience_belongs_to_candidate_and_is_required(
    requirement: str, security: bool
) -> None:
    assert_specialization(operating_vacancy(requirement, security=security), expected=False)


@pytest.mark.parametrize(
    ("requirement", "confirmed", "security"),
    [
        (
            "Знание Helm желательно, а опыт администрирования Kubernetes обязателен.",
            ("Kubernetes",),
            False,
        ),
        (
            "Практический опыт Helm обязателен. Kubernetes — кластер, которым предстоит управлять.",
            ("Kubernetes", "Helm"),
            False,
        ),
        (
            "Обязательно самостоятельное управление инфраструктурой через Terraform.",
            ("Terraform",),
            False,
        ),
        (
            "Опыт эксплуатации Triton обязателен. Сервис работает на GPU.",
            ("Triton",),
            False,
        ),
        (
            "КриптоПро — основное средство. Практический опыт его эксплуатации обязателен.",
            ("CryptoPro",),
            True,
        ),
        ("Практический опыт работы с Crypto Pro обязателен.", ("КриптоПро",), True),
        (
            "Самостоятельно администрировать Kubernetes, "
            "а документацией занимается другая команда.",
            ("Kubernetes",),
            False,
        ),
        (
            "Базовое знакомство с Docker, а опыт промышленной эксплуатации Kubernetes обязателен.",
            ("Kubernetes",),
            False,
        ),
        (
            "Практический опыт управления инфраструктурой через Terraform обязателен, "
            "а Kubernetes не требуется.",
            ("Terraform",),
            False,
        ),
        (
            "Опыт работы с Kubernetes не обязателен, "
            "но обязательна самостоятельная эксплуатация кластера в production.",
            ("Kubernetes",),
            False,
        ),
        (
            "Опыт работы с CryptoPro обязателен, знание SQL необязательно.",
            ("CryptoPro",),
            True,
        ),
        (
            "Другая команда передаст вам администрирование Kubernetes после выхода.",
            ("Kubernetes",),
            False,
        ),
    ],
)
def test_mandatory_operations_and_confirmed_tools_are_paired(
    requirement: str, confirmed: tuple[str, ...], security: bool
) -> None:
    data = operating_vacancy(requirement, security=security)
    assert_specialization(data, expected=True)
    assert_specialization(data, expected=False, confirmed=confirmed)


@pytest.mark.parametrize("separator", ["; ", ". ", ", а ", ", но "])
def test_explicit_requirement_resumes_after_optional_heading(separator: str) -> None:
    data = operating_vacancy("Python.")
    data = replace(
        data,
        description=(
            "Обязанности:\nРазработка автоматизации на Python.\n"
            "Требования:\nPython.\nЖелательно:\nHelm"
            + separator
            + "опыт администрирования Kubernetes обязателен."
        ),
    )
    required = primary_requirements(data.description, data.required_qualifications)
    assert "helm" not in required
    assert "kubernetes" in required
    assert_specialization(data, expected=True)


def test_unlabelled_operations_are_checked_with_the_same_exceptions() -> None:
    data = replace(
        operating_vacancy("Python."),
        description="Обязанности:\nРазработка автоматизации на Python.",
        required_qualifications=None,
    )
    assert (
        unsupported_administration(
            data, "Опыт промышленной эксплуатации Kubernetes не требуется.", ("Python",)
        )
        == ()
    )
    assert unsupported_administration(
        data, "Опыт администрирования Kubernetes обязателен.", ("Python",)
    )


@pytest.mark.parametrize(
    ("requirement", "alternative", "security"),
    [
        (
            "Обязателен опыт управления облачной инфраструктурой: Terraform или Pulumi.",
            "Pulumi",
            False,
        ),
        ("Обязателен опыт эксплуатации Kubernetes или OpenShift.", "OpenShift", False),
        ("Практический опыт работы с CryptoPro или ViPNet.", "ViPNet", True),
    ],
)
def test_confirmed_alternative_only_satisfies_an_explicit_choice(
    requirement: str, alternative: str, security: bool
) -> None:
    data = operating_vacancy(requirement, security=security)
    assert_specialization(data, expected=True)
    assert_specialization(data, expected=False, confirmed=(alternative,))
    combined = operating_vacancy(requirement.replace(" или ", " и "), security=security)
    assert_specialization(combined, expected=True, confirmed=(alternative,))
