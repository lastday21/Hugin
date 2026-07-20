from __future__ import annotations

from hugin.domain.hh import HhResumeDetails
from hugin.domain.vacancies import VacancyRecord
from hugin.services.vacancy_analysis import RuleCategory

TECHNOLOGIES = (
    ("python", "Python"),
    ("fastapi", "FastAPI"),
    ("django", "Django"),
    ("flask", "Flask"),
    ("postgresql", "PostgreSQL"),
    ("redis", "Redis"),
    ("docker", "Docker"),
    ("sqlalchemy", "SQLAlchemy"),
    ("pytest", "Pytest"),
    ("rest", "REST API"),
    ("grpc", "gRPC"),
    ("git", "Git"),
    ("linux", "Linux"),
)


class CoverLetterBuilder:
    def build(
        self,
        vacancy: VacancyRecord,
        resume: HhResumeDetails,
        category: RuleCategory,
    ) -> str:
        vacancy_text = " ".join(
            (
                vacancy.title,
                vacancy.description or "",
                " ".join(vacancy.key_skills),
            )
        ).casefold()
        resume_text = " ".join((resume.title, resume.experience, resume.skills)).casefold()
        matched = [
            label
            for marker, label in TECHNOLOGIES
            if marker in vacancy_text and marker in resume_text
        ][:6]
        technology_sentence = ""
        if matched:
            technology_sentence = " Мой подходящий стек: " + ", ".join(matched) + "."

        specialization_sentence = ""
        if category is RuleCategory.STRETCH and "llm" in resume_text:
            specialization_sentence = " Ранее разрабатывал LLM-прототипы, интегрировал внешние API."

        return (
            "Здравствуйте! Заинтересовала вакансия "
            f"«{vacancy.title}». Практический опыт охватывает Python backend-разработку "
            "и автоматизацию."
            f"{technology_sentence}{specialization_sentence} "
            "Резюме содержит подробное описание рабочих задач и проектов. "
            "Буду рад обсудить, чем могу быть полезен вашей команде."
        )
