from __future__ import annotations

from datetime import UTC, datetime

from hugin.domain.hh import HhResumeDetails
from hugin.domain.vacancies import VacancyRecord
from hugin.services.cover_letter import CoverLetterBuilder
from hugin.services.vacancy_analysis import RuleCategory


def test_cover_letter_uses_only_matching_resume_facts() -> None:
    vacancy = VacancyRecord(
        id=1,
        hh_id="100",
        title="Python backend разработчик",
        source_url="https://hh.ru/vacancy/100",
        employer_name="Компания",
        published_at=None,
        description="FastAPI, PostgreSQL и Docker",
        experience="1-3 года",
        employment="Полная занятость",
        work_format="Удалённо",
        key_skills=("Python", "FastAPI", "PostgreSQL", "Docker"),
        details_fetched_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    resume = HhResumeDetails(
        hh_id="resume-1",
        title="Python backend разработчик",
        experience="FastAPI PostgreSQL Docker LLM",
        skills="Python FastAPI PostgreSQL Docker",
        education="Высшее",
    )

    letter = CoverLetterBuilder().build(vacancy, resume, RuleCategory.MATCH)

    assert vacancy.title in letter
    assert "FastAPI, PostgreSQL, Docker" in letter
    assert "LLM-прототипы" not in letter
