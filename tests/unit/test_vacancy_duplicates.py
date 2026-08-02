from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

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
