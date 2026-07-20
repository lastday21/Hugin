from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from sqlalchemy.orm import Session

from hugin.domain.directions import VacancyState
from hugin.domain.vacancies import VacancyData, VacancyRecord
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.repositories.vacancies import VacancyRepository


class RuleCategory(StrEnum):
    MATCH = "MATCH"
    STRETCH = "STRETCH"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    score: float
    category: RuleCategory
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.category is not RuleCategory.REJECTED


@dataclass(frozen=True, slots=True)
class VacancyAnalysisResult:
    vacancy: VacancyRecord
    evaluation: RuleEvaluation
    state: VacancyState


class PythonBackendRules:
    threshold: ClassVar[float] = 50
    _excluded_specializations: ClassVar[tuple[tuple[str, str], ...]] = (
        ("аналитик", "другое направление: аналитика"),
        ("analyst", "другое направление: аналитика"),
        ("machine learning", "другое направление: машинное обучение"),
        ("ml ", "другое направление: машинное обучение"),
        ("ml-", "другое направление: машинное обучение"),
        ("qa ", "другое направление: проверка программ"),
        ("qa-", "другое направление: проверка программ"),
        ("тестир", "другое направление: проверка программ"),
        ("fullstack", "другое направление: полная разработка"),
        ("mobile", "другое направление: мобильная разработка"),
    )
    _stretch_specializations: ClassVar[tuple[str, ...]] = (
        "ai agent",
        "ai engineer",
        "llm engineer",
        "nlp engineer",
        "rag engineer",
    )
    _seniority_markers: ClassVar[tuple[str, ...]] = ("senior",)
    _useful_skills: ClassVar[tuple[str, ...]] = (
        "fastapi",
        "django",
        "flask",
        "postgresql",
        "sql",
        "docker",
        "git",
        "linux",
        "rest",
        "asyncio",
    )

    def evaluate(self, vacancy: VacancyData) -> RuleEvaluation:
        title = vacancy.title.casefold()
        description = (vacancy.description or "").casefold()
        skills = " ".join(vacancy.key_skills).casefold()
        experience = (vacancy.experience or "").casefold().replace("\N{EN DASH}", "-")
        work_format = (vacancy.work_format or "").casefold()
        complete_text = " ".join((title, description, skills))
        reasons: list[str] = []
        rejected: list[str] = []
        score = 0.0
        stretch = any(marker in title for marker in self._stretch_specializations)

        for marker, reason in self._excluded_specializations:
            if marker in title:
                rejected.append(reason)
                break
        if any(marker in title for marker in self._seniority_markers):
            rejected.append("в названии явно указан уровень Senior")
        if "python" not in complete_text:
            rejected.append("Python не указан в названии, описании или навыках")

        if "python" in title:
            score += 35
            reasons.append("Python указан в названии")
        elif "python" in complete_text:
            score += 15
            reasons.append("Python указан в описании или навыках")
        if "backend" in title or "back-end" in title or "бэкенд" in title:
            score += 25
            reasons.append("серверная разработка указана в названии")
        elif "backend" in description or "back-end" in description or "бэкенд" in description:
            score += 10
            reasons.append("серверная разработка указана в описании")
        if any(marker in title for marker in ("разработчик", "developer", "программист")):
            score += 15
            reasons.append("роль разработчика указана в названии")
        if "не требуется" in experience:
            score += 10
            reasons.append("опыт не требуется")
        elif "1-3" in experience:
            score += 7
            reasons.append("требуемый опыт от одного до трёх лет")
        elif "3-6" in experience or "от 3" in experience:
            reasons.append("опыт от трёх лет указан как пожелание; это не запрет")
        if "удален" in work_format or "удалён" in work_format:
            score += 3
            reasons.append("доступна удалённая работа")

        matched_skills = [skill for skill in self._useful_skills if skill in complete_text]
        if matched_skills:
            score += min(len(matched_skills) * 2, 12)
            reasons.append("подходящие технологии: " + ", ".join(matched_skills))

        score = min(score, 100)
        if stretch and not rejected:
            reasons.append("профильная работа по LLM или NLP; требуется дополнительная подготовка")
        elif score < self.threshold and not rejected:
            rejected.append(f"оценка ниже порога {self.threshold:.0f}")
        reasons.extend(rejected)
        if rejected:
            category = RuleCategory.REJECTED
        elif stretch:
            category = RuleCategory.STRETCH
        else:
            category = RuleCategory.MATCH
        return RuleEvaluation(
            score=score,
            category=category,
            reasons=tuple(reasons),
        )


class VacancyAnalysisService:
    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._directions = DirectionRepository(session)
        self._vacancies = VacancyRepository(session)
        self._rules = PythonBackendRules()

    def pending(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        limit: int,
    ) -> tuple[VacancyRecord, ...]:
        account = self._accounts.get_by_external_id(account_external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден в базе")
        direction = self._directions.get_by_account_and_name(account.id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")
        return tuple(self._vacancies.list_pending_for_direction(direction.id, limit=limit))

    def synchronize(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        vacancies: tuple[VacancyData, ...],
    ) -> tuple[VacancyAnalysisResult, ...]:
        account = self._accounts.get_by_external_id(account_external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден в базе")
        direction = self._directions.get_by_account_and_name(account.id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")

        results: list[VacancyAnalysisResult] = []
        for vacancy in vacancies:
            stored = self._vacancies.upsert(vacancy)
            results.append(self._apply(direction.id, stored, vacancy))
        return tuple(results)

    def reanalyze(
        self,
        *,
        account_external_id: str,
        direction_name: str,
    ) -> tuple[VacancyAnalysisResult, ...]:
        account = self._accounts.get_by_external_id(account_external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден в базе")
        direction = self._directions.get_by_account_and_name(account.id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")

        results: list[VacancyAnalysisResult] = []
        for stored in self._vacancies.list_detailed_for_direction(direction.id):
            vacancy = VacancyData(
                hh_id=stored.hh_id,
                title=stored.title,
                source_url=stored.source_url,
                employer_name=stored.employer_name,
                published_at=stored.published_at,
                description=stored.description,
                experience=stored.experience,
                employment=stored.employment,
                work_format=stored.work_format,
                key_skills=stored.key_skills,
                details_fetched_at=stored.details_fetched_at,
            )
            results.append(self._apply(direction.id, stored, vacancy))
        return tuple(results)

    def _apply(
        self,
        direction_id: int,
        stored: VacancyRecord,
        vacancy: VacancyData,
    ) -> VacancyAnalysisResult:
        evaluation = self._rules.evaluate(vacancy)
        state = VacancyState.FILTERED if evaluation.accepted else VacancyState.SKIPPED
        self._directions.apply_rules(
            direction_id,
            stored.id,
            state=state,
            score=evaluation.score,
            details={
                "accepted": evaluation.accepted,
                "category": evaluation.category.value,
                "reasons": list(evaluation.reasons),
                "threshold": self._rules.threshold,
            },
        )
        return VacancyAnalysisResult(stored, evaluation, state)
