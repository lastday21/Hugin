from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy.orm import Session

from hugin.domain.directions import (
    AccountRecord,
    DirectionRecord,
    SearchRegion,
    VacancyState,
    WorkFormat,
)
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyRecord
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.career_directions import CareerDirectionService
from hugin.services.vacancy_duplicates import VacancyDuplicateDetector

RULES_VERSION = "python_it_v2"


class RuleCategory(StrEnum):
    MATCH = "MATCH"
    STRETCH = "STRETCH"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class RuleContext:
    skills: tuple[str, ...] = ()
    work_formats: tuple[WorkFormat, ...] = ()
    regions: tuple[SearchRegion, ...] = ()
    minimum_salary: int | None = None
    desired_salary: int | None = None


@dataclass(frozen=True, slots=True)
class RuleComponent:
    name: str
    score: float
    weight: float
    reason: str


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    score: float
    category: RuleCategory
    reasons: tuple[str, ...]
    components: tuple[RuleComponent, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.category is not RuleCategory.REJECTED


@dataclass(frozen=True, slots=True)
class VacancyAnalysisResult:
    vacancy: VacancyRecord
    evaluation: RuleEvaluation
    state: VacancyState


class PythonBackendRules:
    soft_boundary: ClassVar[float] = 50
    _excluded_specializations: ClassVar[tuple[tuple[str, str], ...]] = (
        ("аналитик", "другое направление: аналитика"),
        ("analyst", "другое направление: аналитика"),
        ("machine learning", "другое направление: машинное обучение"),
        ("ml engineer", "другое направление: машинное обучение"),
        ("ручной тестировщик", "работа не связана с написанием кода"),
        ("manual qa", "работа не связана с написанием кода"),
        ("fullstack", "другое основное направление: полная разработка"),
        ("mobile", "другое основное направление: мобильная разработка"),
    )
    _stretch_specializations: ClassVar[tuple[str, ...]] = (
        "ai agent",
        "ai engineer",
        "llm engineer",
        "nlp engineer",
        "rag engineer",
    )
    _development_markers: ClassVar[tuple[str, ...]] = (
        "python",
        "backend",
        "back-end",
        "бэкенд",
        "разработ",
        "developer",
        "програм",
        "автоматизац",
        "automation",
        "интеграц",
        " api",
        "etl",
    )
    _useful_skills: ClassVar[tuple[str, ...]] = (
        "python",
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
    _scam_markers: ClassVar[tuple[str, ...]] = (
        "оплатить обучение",
        "вступительный взнос",
        "код из смс",
        "данные банковской карты",
        "паспортные данные для регистрации",
    )

    def evaluate(
        self,
        vacancy: VacancyData,
        context: RuleContext | None = None,
    ) -> RuleEvaluation:
        context = context or RuleContext()
        title = vacancy.title.casefold()
        description = (vacancy.description or "").casefold()
        responsibilities = (vacancy.responsibilities or "").casefold()
        requirements = (vacancy.required_qualifications or "").casefold()
        skills = " ".join(vacancy.key_skills).casefold()
        complete_text = " ".join((title, description, responsibilities, requirements, skills))
        experience = (vacancy.experience or "").casefold().replace("\N{EN DASH}", "-")
        reasons: list[str] = []
        rejected: list[str] = []
        components: list[RuleComponent] = []

        if vacancy.availability is not VacancyAvailability.ACTIVE:
            rejected.append(f"вакансия недоступна: {vacancy.availability.value}")
        scam = next((marker for marker in self._scam_markers if marker in complete_text), None)
        if scam is not None:
            rejected.append(f"подозрительное требование: {scam}")

        has_development = any(marker in complete_text for marker in self._development_markers)
        for marker, reason in self._excluded_specializations:
            if marker in title and not self._has_secondary_development_role(title):
                rejected.append(reason)
                break
        if not has_development:
            rejected.append("работа не связана с написанием кода или технической автоматизацией")

        profile_tokens = self._profile_skill_tokens(context.skills)
        vacancy_tokens = self._tokens(" ".join((complete_text, skills)))
        skill_overlap = sorted(profile_tokens & vacancy_tokens)
        if context.skills:
            if (
                profile_tokens
                and not skill_overlap
                and self._explicit_other_stack(title, requirements)
            ):
                rejected.append("обязательные технологии не связаны с подтверждённым опытом")
        elif "python" not in complete_text:
            rejected.append("Python не указан в названии, описании или навыках")

        senior_responsibility = any(
            marker in " ".join((responsibilities, requirements))
            for marker in (
                "руководство команд",
                "управление команд",
                "найм разработчик",
                "ответственность за архитектуру",
                "technical leadership",
                "manage a team",
            )
        )
        if "senior" in title and senior_responsibility:
            rejected.append("Senior-позиция с явно повышенной ответственностью")
        elif any(marker in title for marker in ("senior", "lead", "principal", "ведущ")):
            reasons.append(
                "уровень роли учтён как риск, но не блокирует отклик без анализа обязанностей"
            )

        if self._relocation_conflicts(complete_text, context):
            rejected.append("обязательный переезд указан вне выбранных городов")

        format_score = self._work_format_score(vacancy, context)
        if format_score is not None:
            if format_score == 0:
                rejected.append("обязательный формат работы противоречит настройкам")
            else:
                self._component(components, reasons, "format", format_score, 10, "формат работы")

        region_score = self._region_score(vacancy, context)
        if region_score is not None:
            self._component(components, reasons, "region", region_score, 10, "регион")

        role_score = self._role_score(title, complete_text)
        self._component(components, reasons, "role", role_score, 35, "название и обязанности")
        if "python" in title:
            reasons.append("Python указан в названии")

        if profile_tokens and vacancy_tokens:
            profile_score = min(35 + len(skill_overlap) * 13, 100) if skill_overlap else 20
            detail = (
                "совпали подтверждённые навыки: " + ", ".join(skill_overlap[:8])
                if skill_overlap
                else "подтверждённые навыки явно не перечислены"
            )
            self._component(components, reasons, "skills", profile_score, 25, detail)
        else:
            matched = [skill for skill in self._useful_skills if skill in complete_text]
            if matched:
                generic_score = min(45 + len(matched) * 10, 100)
                self._component(
                    components,
                    reasons,
                    "skills",
                    generic_score,
                    25,
                    "подходящие технологии: " + ", ".join(matched),
                )

        experience_score = self._experience_score(experience)
        if experience_score is not None:
            experience_reason = "требования к опыту не являются самостоятельным запретом"
            if "3-6" in experience or "от 3" in experience:
                experience_reason = "опыт от трёх лет указан как пожелание; это не запрет"
            self._component(
                components,
                reasons,
                "experience",
                experience_score,
                10,
                experience_reason,
            )

        salary_score = self._salary_score(vacancy, context)
        if salary_score is not None:
            self._component(components, reasons, "salary", salary_score, 10, "зарплата")

        freshness_score = self._freshness_score(vacancy.published_at)
        if freshness_score is not None:
            self._component(components, reasons, "freshness", freshness_score, 5, "свежесть")

        description_score = self._description_score(vacancy)
        if description_score is not None:
            self._component(
                components,
                reasons,
                "description",
                description_score,
                15,
                "полнота описания",
            )

        score = self._weighted_score(components)
        stretch = any(marker in title for marker in self._stretch_specializations)
        if rejected:
            category = RuleCategory.REJECTED
        elif stretch:
            category = RuleCategory.STRETCH
            reasons.append(
                "профильная работа по LLM или NLP; потребуется дополнительная подготовка"
            )
        else:
            category = RuleCategory.MATCH
            if score < self.soft_boundary:
                reasons.append(
                    f"мягкая оценка ниже {self.soft_boundary:.0f}; "
                    "это влияет только на порядок очереди"
                )
        reasons.extend(rejected)
        return RuleEvaluation(score, category, tuple(dict.fromkeys(reasons)), tuple(components))

    @staticmethod
    def _component(
        components: list[RuleComponent],
        reasons: list[str],
        name: str,
        score: float,
        weight: float,
        reason: str,
    ) -> None:
        components.append(RuleComponent(name, score, weight, reason))
        reasons.append(f"{reason}: {score:.0f}")

    @staticmethod
    def _weighted_score(components: list[RuleComponent]) -> float:
        weight = sum(component.weight for component in components)
        if not weight:
            return 0.0
        weighted = sum(component.score * component.weight for component in components)
        return round(weighted / weight, 2)

    @staticmethod
    def _has_secondary_development_role(title: str) -> bool:
        markers = ("разработ", "developer", "automation", "автоматизац")
        return any(marker in title for marker in markers)

    @staticmethod
    def _explicit_other_stack(title: str, requirements: str) -> bool:
        text = " ".join((title, requirements))
        return any(
            marker in text
            for marker in (
                "golang",
                "go developer",
                "java developer",
                "php developer",
                "1с разработ",
            )
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-zа-яё][a-zа-яё0-9+#.-]{1,}", text.casefold())
            if len(token) > 1
        }

    def _profile_skill_tokens(self, values: tuple[str, ...]) -> set[str]:
        return self._tokens(" ".join(values))

    @staticmethod
    def _role_score(title: str, text: str) -> float:
        if "python" in title and any(marker in title for marker in ("backend", "бэкенд")):
            return 100
        if "python" in title:
            return 85
        if "python" in text and any(marker in text for marker in ("backend", "бэкенд")):
            return 70
        if "python" in text and "api" in text:
            return 65
        if "python" in text:
            return 45
        return 35

    @staticmethod
    def _experience_score(experience: str) -> float | None:
        if not experience:
            return None
        if "не требуется" in experience:
            return 100
        if "1-3" in experience or "1–3" in experience:
            return 90
        if "3-6" in experience or "3–6" in experience or "от 3" in experience:
            return 65
        return 55

    @staticmethod
    def _work_format_score(vacancy: VacancyData, context: RuleContext) -> float | None:
        if not context.work_formats or not vacancy.work_format:
            return None
        value = vacancy.work_format.casefold()
        vacancy_formats: set[WorkFormat] = set()
        if "удал" in value or "remote" in value:
            vacancy_formats.add(WorkFormat.REMOTE)
        if "офис" in value or "на месте" in value or "on-site" in value:
            vacancy_formats.add(WorkFormat.ON_SITE)
        if "гибрид" in value or "hybrid" in value:
            vacancy_formats.add(WorkFormat.HYBRID)
        if not vacancy_formats:
            return None
        return 100 if vacancy_formats & set(context.work_formats) else 0

    @staticmethod
    def _relocation_conflicts(text: str, context: RuleContext) -> bool:
        mandatory = any(
            marker in text
            for marker in (
                "обязательным условием является релокация",
                "обязательная релокация",
                "обязательный переезд",
                "переезд обязателен",
            )
        )
        if not mandatory or not context.regions:
            return False
        return not any(region.name.casefold() in text for region in context.regions)

    @staticmethod
    def _salary_score(vacancy: VacancyData, context: RuleContext) -> float | None:
        target = context.desired_salary or context.minimum_salary
        offered = vacancy.salary_to or vacancy.salary_from
        if target is None or offered is None or vacancy.salary_currency not in {None, "RUR", "RUB"}:
            return None
        ratio = float(offered) / target
        return min(max(ratio * 100, 20), 100)

    @staticmethod
    def _region_score(vacancy: VacancyData, context: RuleContext) -> float | None:
        if not context.regions or not vacancy.region:
            return None
        work_format = (vacancy.work_format or "").casefold()
        if "удал" in work_format or "remote" in work_format:
            return 100
        vacancy_region = vacancy.region.casefold()
        return (
            100
            if any(region.name.casefold() in vacancy_region for region in context.regions)
            else 20
        )

    @staticmethod
    def _freshness_score(published_at: datetime | None) -> float | None:
        if published_at is None:
            return None
        age_seconds = (datetime.now(UTC) - published_at.astimezone(UTC)).total_seconds()
        age_days = max(age_seconds / 86400, 0)
        if age_days <= 2:
            return 100
        if age_days <= 7:
            return 80
        if age_days <= 30:
            return 55
        return 30

    @staticmethod
    def _description_score(vacancy: VacancyData) -> float | None:
        if not vacancy.description:
            return None
        score = 35
        if len(vacancy.description) >= 500:
            score += 25
        if vacancy.responsibilities:
            score += 15
        if vacancy.required_qualifications:
            score += 15
        if vacancy.key_skills:
            score += 10
        return min(score, 100)


class VacancyAnalysisService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)
        self._directions = DirectionRepository(session)
        self._vacancies = VacancyRepository(session)
        self._rules = PythonBackendRules()
        self._duplicates = VacancyDuplicateDetector()

    def pending(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        limit: int,
    ) -> tuple[VacancyRecord, ...]:
        _, direction = self._account_and_direction(account_external_id, direction_name)
        return tuple(self._vacancies.list_pending_for_direction(direction.id, limit=limit))

    def synchronize(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        vacancies: tuple[VacancyData, ...],
    ) -> tuple[VacancyAnalysisResult, ...]:
        account, direction = self._account_and_direction(account_external_id, direction_name)
        context = self._context(account.id, direction_name)
        results: list[VacancyAnalysisResult] = []
        for vacancy in vacancies:
            stored = self._vacancies.upsert(vacancy)
            self._directions.track_vacancy(direction.id, stored.id)
            results.append(self._apply(direction.id, stored, vacancy, context))
        return tuple(results)

    def reanalyze(
        self,
        *,
        account_external_id: str,
        direction_name: str,
    ) -> tuple[VacancyAnalysisResult, ...]:
        account, direction = self._account_and_direction(account_external_id, direction_name)
        context = self._context(account.id, direction_name)
        results: list[VacancyAnalysisResult] = []
        for stored in self._vacancies.list_detailed_for_direction(direction.id):
            vacancy = self._data(stored)
            results.append(self._apply(direction.id, stored, vacancy, context))
        return tuple(results)

    def _apply(
        self,
        direction_id: int,
        stored: VacancyRecord,
        vacancy: VacancyData,
        context: RuleContext,
    ) -> VacancyAnalysisResult:
        tracked = self._directions.get_tracked_vacancy(direction_id, stored.id)
        if tracked.rules_details.get("manual_override") == "ACCEPT":
            raw_reasons = tracked.rules_details.get("reasons", [])
            reason_values = raw_reasons if isinstance(raw_reasons, list) else []
            reasons = tuple(str(item) for item in reason_values)
            evaluation = RuleEvaluation(
                tracked.rules_score or 50,
                RuleCategory.MATCH,
                reasons or ("решение изменено пользователем",),
            )
            return VacancyAnalysisResult(stored, evaluation, VacancyState.ANALYZED)

        candidates = self._vacancies.list_duplicate_candidates(stored)
        duplicate = self._duplicates.find(stored, candidates)
        if duplicate is not None:
            stored = self._vacancies.mark_duplicate(
                stored.id,
                duplicate.canonical.id,
                duplicate.similarity,
            )

        evaluation = self._rules.evaluate(vacancy, context)
        if duplicate is not None:
            evaluation = RuleEvaluation(
                evaluation.score,
                evaluation.category,
                (
                    *evaluation.reasons,
                    "найдена похожая публикация той же компании; вакансия обрабатывается отдельно",
                    f"связанная вакансия: {duplicate.canonical.hh_id}",
                ),
                evaluation.components,
            )
        if vacancy.availability is not VacancyAvailability.ACTIVE:
            state = VacancyState.CLOSED
        else:
            state = VacancyState.ANALYZED if evaluation.accepted else VacancyState.FILTERED_OUT

        self._directions.apply_rules(
            direction_id,
            stored.id,
            state=state,
            score=evaluation.score,
            details={
                "accepted": evaluation.accepted,
                "category": evaluation.category.value,
                "reasons": list(evaluation.reasons),
                "components": [
                    {
                        "name": component.name,
                        "score": component.score,
                        "weight": component.weight,
                        "reason": component.reason,
                    }
                    for component in evaluation.components
                ],
                "soft_boundary": self._rules.soft_boundary,
                "duplicate_of_id": stored.duplicate_of_id,
            },
            rules_version=RULES_VERSION,
        )
        return VacancyAnalysisResult(stored, evaluation, state)

    def _account_and_direction(
        self,
        external_id: str,
        direction_name: str,
    ) -> tuple[AccountRecord, DirectionRecord]:
        account = self._accounts.get_by_external_id(external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru не найден в базе")
        direction = self._directions.get_by_account_and_name(account.id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")
        return account, direction

    def _context(self, account_id: int, direction_name: str) -> RuleContext:
        try:
            settings = CareerDirectionService(self._session).get(account_id, direction_name)
        except LookupError:
            return RuleContext()
        regions = tuple(
            {region.area: region for query in settings.queries for region in query.regions}.values()
        )
        return RuleContext(
            skills=settings.skills_from_resume,
            work_formats=settings.work_formats,
            regions=regions,
            minimum_salary=settings.minimum_salary,
            desired_salary=settings.desired_salary,
        )

    @staticmethod
    def _data(stored: VacancyRecord) -> VacancyData:
        return VacancyData(
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
            region=stored.region,
            address=stored.address,
            salary_from=stored.salary_from,
            salary_to=stored.salary_to,
            salary_currency=stored.salary_currency,
            salary_gross=stored.salary_gross,
            schedule=stored.schedule,
            responsibilities=stored.responsibilities,
            required_qualifications=stored.required_qualifications,
            preferred_qualifications=stored.preferred_qualifications,
            has_cover_letter=stored.has_cover_letter,
            has_screening_form=stored.has_screening_form,
            has_external_link=stored.has_external_link,
            has_test_assignment=stored.has_test_assignment,
            availability=stored.availability,
        )
