from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hugin.domain.vacancies import VacancyRecord
from hugin.services.vacancy_duplicates import VacancyDuplicateDetector


def _vacancy(
    *,
    vacancy_id: int = 1,
    employer: str | None = "Компания",
    description: str | None = "Разработка сервиса на Python и PostgreSQL.",
    salary_from: Decimal | None = None,
    salary_to: Decimal | None = None,
    currency: str | None = None,
) -> VacancyRecord:
    return VacancyRecord(
        id=vacancy_id,
        hh_id=f"vacancy-{vacancy_id}",
        title="Python-разработчик",
        source_url=f"https://hh.ru/vacancy/{vacancy_id}",
        employer_name=employer,
        published_at=None,
        description=description,
        experience=None,
        employment=None,
        work_format=None,
        key_skills=(),
        details_fetched_at=None,
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=currency,
    )


def test_duplicate_detector_skips_unrelated_and_weaker_candidates() -> None:
    detector = VacancyDuplicateDetector()
    vacancy = _vacancy()
    unrelated = _vacancy(vacancy_id=2, employer="Другая компания")
    duplicate = _vacancy(vacancy_id=3)
    same_duplicate = replace(duplicate, id=4, hh_id="vacancy-4")

    match = detector.find(vacancy, [unrelated, duplicate, same_duplicate])

    assert match is not None
    assert match.canonical.id == duplicate.id
    assert detector.find(vacancy, [unrelated]) is None


def test_duplicate_detector_handles_empty_text_and_salary_edges() -> None:
    detector = VacancyDuplicateDetector()
    no_salary = _vacancy(description=None)
    rubles = _vacancy(
        vacancy_id=2,
        salary_from=Decimal("120000"),
        salary_to=Decimal("120000"),
        currency="RUR",
    )
    dollars = _vacancy(
        vacancy_id=3,
        salary_from=Decimal("120000"),
        salary_to=Decimal("120000"),
        currency="USD",
    )

    assert detector._text_similarity("", "Python") == 0.0
    assert detector._salary_compatible(no_salary, rubles)
    assert detector._salary_similarity(no_salary, rubles) == 0.5
    assert detector._salary_similarity(rubles, rubles) == 1.0
    assert not detector._salary_compatible(rubles, dollars)


@pytest.mark.parametrize("title", ["Java-разработчик", "Тестировщик", "Системный администратор"])
def test_identical_company_description_does_not_merge_different_professions(title: str) -> None:
    vacancy = _vacancy(description="Разработка внутренних сервисов компании.")
    other = replace(vacancy, id=2, hh_id="2", title=title)
    assert VacancyDuplicateDetector().find(vacancy, [other]) is None


def test_same_title_with_conflicting_mandatory_languages_is_not_duplicate() -> None:
    vacancy = replace(_vacancy(), title="Разработчик", required_qualifications="Python и FastAPI.")
    other = replace(vacancy, id=2, hh_id="2", required_qualifications="Java и Spring.")
    assert VacancyDuplicateDetector().find(vacancy, [other]) is None


def test_renamed_publication_keeps_duplicate_protection() -> None:
    vacancy = _vacancy()
    other = replace(vacancy, id=2, hh_id="2", title="Разработчик серверных сервисов Python")
    assert VacancyDuplicateDetector().find(vacancy, [other]) is not None


def test_optional_foreign_language_does_not_split_real_duplicate() -> None:
    vacancy = replace(_vacancy(), required_qualifications="Требования: Python. Будет плюсом: Java.")
    other = replace(vacancy, id=2, hh_id="2", required_qualifications="Требования: Python.")
    assert VacancyDuplicateDetector().find(vacancy, [other]) is not None


def test_wrong_stored_duties_from_company_offer_do_not_merge_distinct_roles() -> None:
    offer = "Мы предлагаем:\nЗадачи по развитию сервисов.\nКонкурентная зарплата."
    developer = replace(
        _vacancy(),
        title="Инженер",
        responsibilities="Конкурентная зарплата.",
        description="Чем ты будешь заниматься:\nРазрабатывать интерфейсы на C++ и Qt5.\n" + offer,
    )
    tester = replace(
        developer,
        id=2,
        hh_id="2",
        description="Чем ты будешь заниматься:\nПроводить тесты на Python и pytest.\n" + offer,
    )
    detector = VacancyDuplicateDetector()
    assert detector.find(developer, [tester]) is None
    assert detector.find(developer, [replace(developer, id=3, hh_id="3")]) is not None


def test_generic_title_with_shared_duties_keeps_different_mandatory_languages_separate() -> None:
    vacancy = replace(
        _vacancy(),
        title="Разработчик",
        description="Обязанности:\nРазработка внутренних сервисов.\nТребования:\nPython и Java.",
    )
    other = replace(
        vacancy,
        id=2,
        hh_id="2",
        description="Обязанности:\nРазработка внутренних сервисов.\nТребования:\nPython.",
    )
    assert VacancyDuplicateDetector().find(vacancy, [other]) is None


def test_missing_body_or_changed_salary_alone_does_not_unlink_existing_duplicate() -> None:
    detector = VacancyDuplicateDetector()
    vacancy = _vacancy(salary_from=Decimal("100000"), salary_to=Decimal("120000"))
    assert detector.conflict_reason(vacancy, replace(vacancy, description=None)) is None
    assert (
        detector.conflict_reason(
            vacancy, replace(vacancy, salary_from=Decimal("200000"), salary_to=Decimal("250000"))
        )
        is None
    )
    assert detector.conflict_reason(vacancy, replace(vacancy, employer_name="Другая компания")) == (
        "different_employers"
    )
    assert (
        detector.conflict_reason(
            vacancy, replace(vacancy, description="Обязанности:\nВести бухгалтерский учет.")
        )
        == "different_responsibilities"
    )


def test_one_of_several_languages_is_an_alternative_not_a_mandatory_stack() -> None:
    vacancy = replace(
        _vacancy(),
        title="Руководитель команды разработки",
        required_qualifications="Опыт разработки на одном из языков: Java, Python, Go, C++.",
    )
    renamed = replace(
        vacancy,
        id=2,
        hh_id="2",
        required_qualifications=(
            "Опыт разработки на одном из языков: Java, Python, Go, C++, JavaScript."
        ),
    )
    detector = VacancyDuplicateDetector()
    assert detector.conflict_reason(vacancy, renamed) is None
    assert detector.find(vacancy, [renamed]) is not None
    different = replace(
        renamed, required_qualifications=("Опыт разработки на одном из языков: PHP, Ruby.")
    )
    assert detector.find(vacancy, [different]) is None


def test_cyrillic_cpp_title_is_not_treated_as_an_unspecified_developer() -> None:
    vacancy = replace(_vacancy(), title="Разработчик С++")
    other = replace(vacancy, id=2, hh_id="2", title="Разработчик Java")
    assert VacancyDuplicateDetector().find(vacancy, [other]) is None
