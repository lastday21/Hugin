from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import CandidateProfileModel, VerifiedFactModel
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import (
    AccountRecord,
    DirectionRecord,
    DirectionScope,
    SearchRegion,
    VacancyState,
    WorkFormat,
)
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyRecord
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.career_directions import CareerDirectionService
from hugin.services.vacancy_duplicates import VacancyDuplicateDetector

RULES_VERSION = "python_it_v6"


class RuleCategory(StrEnum):
    MATCH = "MATCH"
    STRETCH = "STRETCH"
    REJECTED = "REJECTED"
    ROUTED = "ROUTED"


@dataclass(frozen=True, slots=True)
class RuleContext:
    skills: tuple[str, ...] = ()
    work_formats: tuple[WorkFormat, ...] = ()
    regions: tuple[SearchRegion, ...] = ()
    minimum_salary: int | None = None
    desired_salary: int | None = None
    relocation_allowed: bool | None = None


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
    target_scope: DirectionScope | None = None

    @property
    def accepted(self) -> bool:
        return self.category in {RuleCategory.MATCH, RuleCategory.STRETCH}


@dataclass(frozen=True, slots=True)
class VacancyAnalysisResult:
    vacancy: VacancyRecord
    evaluation: RuleEvaluation
    state: VacancyState


class VacancyRoleRouter:
    _adjacent_title_markers: ClassVar[tuple[str, ...]] = (
        "fullstack",
        "full-stack",
        "full stack",
        "фулстек",
        "automation",
        "автоматизац",
        "интеграц",
        "integration",
        "автотест",
        "qa automation",
        "test automation",
        "etl",
        "data engineer",
        "инженер данных",
        "devops",
        "sre",
        "platform engineer",
        "llm",
        "rag",
        "ai agent",
        "ai engineer",
        "nlp engineer",
        "ml engineer",
        "ml-инженер",
        "ml инженер",
        "ml lead",
        "machine learning",
    )
    _backend_markers: ClassVar[tuple[str, ...]] = (
        "backend",
        "back-end",
        "бэкенд",
        "fastapi",
        "django",
        "flask",
        "серверн",
        "микросервис",
        "rest api",
        "backend api",
    )
    _developer_markers: ClassVar[tuple[str, ...]] = (
        "разработ",
        "developer",
        "engineer",
        "инженер",
        "программист",
    )

    @classmethod
    def classify(cls, vacancy: VacancyData) -> DirectionScope | None:
        title = vacancy.title.casefold()
        complete_text = " ".join(
            (
                title,
                (vacancy.description or "").casefold(),
                (vacancy.responsibilities or "").casefold(),
                (vacancy.required_qualifications or "").casefold(),
                " ".join(vacancy.key_skills).casefold(),
            )
        )
        testing_title = any(
            marker in title for marker in ("тестирован", "тестировщик", "qa engineer", "qa-инженер")
        )
        automated_testing = any(
            marker in complete_text
            for marker in (
                "автоматизац",
                "автотест",
                "test automation",
                "locust",
                "pytest",
            )
        )
        if testing_title and automated_testing and "python" in complete_text:
            return DirectionScope.IT_ADJACENT
        if any(marker in title for marker in cls._adjacent_title_markers):
            return DirectionScope.IT_ADJACENT
        has_python = "python" in complete_text
        has_backend = any(marker in complete_text for marker in cls._backend_markers)
        if (
            has_python
            and has_backend
            and (any(marker in title for marker in cls._backend_markers) or "python" in title)
        ):
            return DirectionScope.PYTHON_BACKEND
        if "python" in title and any(marker in title for marker in cls._developer_markers):
            return DirectionScope.IT_ADJACENT
        if any(marker in title for marker in cls._adjacent_title_markers):
            return DirectionScope.IT_ADJACENT
        return None


class PythonBackendRules:
    soft_boundary: ClassVar[float] = 50
    scope: ClassVar[DirectionScope] = DirectionScope.PYTHON_BACKEND
    requires_python_without_profile: ClassVar[bool] = True
    _excluded_specializations: ClassVar[tuple[tuple[str, str], ...]] = (
        ("аналитик", "другое направление: аналитика"),
        ("analyst", "другое направление: аналитика"),
        ("machine learning", "другое направление: машинное обучение"),
        ("ml engineer", "другое направление: машинное обучение"),
        ("ручной тестировщик", "работа не связана с написанием кода"),
        ("manual qa", "работа не связана с написанием кода"),
        ("тестирован", "другое направление: проверка качества"),
        ("qa engineer", "другое направление: проверка качества"),
        ("qa-инженер", "другое направление: проверка качества"),
        ("тестировщик", "другое направление: проверка качества"),
        ("fullstack", "другое основное направление: полная разработка"),
        ("mobile", "другое основное направление: мобильная разработка"),
        ("frontend", "другое основное направление: клиентская разработка"),
        ("front-end", "другое основное направление: клиентская разработка"),
        ("фронтенд", "другое основное направление: клиентская разработка"),
        ("embedded", "другое основное направление: встроенные системы"),
        ("встраиваем", "другое основное направление: встроенные системы"),
        ("информационной безопасности", "другое основное направление: безопасность"),
        ("information security", "другое основное направление: безопасность"),
        ("безопасност", "другое основное направление: безопасность"),
        ("security", "другое основное направление: безопасность"),
        ("системный администратор", "другое основное направление: системное администрирование"),
        ("ит-инфраструктур", "другое основное направление: ИТ-инфраструктура"),
        ("серверным платформ", "другое основное направление: ИТ-инфраструктура"),
        ("robotics", "другое основное направление: робототехника"),
        ("робототех", "другое основное направление: робототехника"),
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
        "fullstack",
        "full-stack",
        "devops",
        "автотест",
        "test automation",
        "llm",
        "rag",
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

        destination = VacancyRoleRouter.classify(vacancy)
        if not rejected and destination is not None and destination is not self.scope:
            return RuleEvaluation(
                score=0,
                category=RuleCategory.ROUTED,
                reasons=(
                    "перенесена в другое направление: "
                    + ("Python backend" if destination is DirectionScope.PYTHON_BACKEND else "ИТ"),
                ),
                target_scope=destination,
            )

        has_development = any(marker in complete_text for marker in self._development_markers)
        for marker, reason in self._excluded_specializations:
            if marker in title:
                if marker in {
                    "тестирован",
                    "qa engineer",
                    "qa-инженер",
                    "тестировщик",
                } and any(
                    automation in complete_text
                    for automation in (
                        "автоматизац",
                        "автотест",
                        "automation",
                        "locust",
                        "pytest",
                    )
                ):
                    continue
                rejected.append(reason)
                break
        other_stack = self._primary_other_stack(title)
        if other_stack is not None and "python" not in title:
            rejected.append(f"другой основной стек в названии: {other_stack}")
        mandatory_other_stack = self._mandatory_other_stack(" ".join((requirements, description)))
        if mandatory_other_stack is not None:
            rejected.append(f"другой обязательный основной стек: {mandatory_other_stack}")
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
        elif self.requires_python_without_profile and "python" not in complete_text:
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
        elif any(
            marker in title
            for marker in (
                "техлид",
                "архитектор",
                "architect",
                "руководитель",
                "head of",
            )
        ):
            reasons.append(
                "руководящая или архитектурная составляющая учтена как риск, "
                "но само название не блокирует вакансию"
            )

        if self._relocation_conflicts(complete_text, context):
            rejected.append("обязательный переезд противоречит подтверждённым настройкам")

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
                "c# developer",
                ".net developer",
                "разработчик c#",
                "разработчик .net",
                "node.js",
                "nodejs",
                "nestjs",
                "c++",
                "rust",
            )
        )

    @staticmethod
    def _primary_other_stack(title: str) -> str | None:
        patterns = (
            (r"\bjava\b", "Java"),
            (r"(?<!\w)c#(?!\w)|(?<!\w)\.net(?!\w)|\bdotnet\b", "C#/.NET"),
            (r"\bgolang\b|\bgo\b", "Go"),
            (r"\bnode(?:\.js)?\b|\bnestjs\b", "Node.js"),
            (r"\bphp\b", "PHP"),
            (r"(?<!\w)c\+\+(?!\w)", "C++"),
            (r"\b1[сc]\b", "1С"),
            (r"\brust\b", "Rust"),
            (r"\btypescript\b|\bjavascript\b", "JavaScript/TypeScript"),
            (r"\breact\b|\bvue(?:\.js)?\b|\bangular\b", "клиентский JavaScript"),
        )
        return next(
            (label for pattern, label in patterns if re.search(pattern, title) is not None),
            None,
        )

    @staticmethod
    def _mandatory_other_stack(text: str) -> str | None:
        stack_patterns = (
            (
                r"(?:(?:node(?:\.js)?|nodejs|nestjs|typescript|\bts\b)"
                r".{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}"
                r"(?:node(?:\.js)?|nodejs|nestjs|typescript|\bts\b))",
                "Node.js/TypeScript",
            ),
            (
                r"(?:\bjava\b.{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}\bjava\b)",
                "Java",
            ),
            (
                r"(?:(?:c#|\.net).{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}(?:c#|\.net))",
                "C#/.NET",
            ),
            (
                r"(?:(?:golang|\bgo\b).{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}(?:golang|\bgo\b))",
                "Go",
            ),
            (
                r"(?:\brust\b.{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}\brust\b)",
                "Rust",
            ),
            (
                r"(?:(?:1[сc])\b.{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}(?:1[сc])\b)",
                "1С",
            ),
        )
        primary_stack = next(
            (label for pattern, label in stack_patterns if re.search(pattern, text) is not None),
            None,
        )
        if primary_stack is not None:
            return primary_stack
        if (
            re.search(
                r"\bpython\b.{0,60}(?:(?<!не\s)только|лишь|legacy|вспомогательн|"
                r"границ\w*\s+(?:ml|ai)|пример\w*\s+базов\w+\s+язык)",
                text,
            )
            is not None
        ):
            return "Python указан только как вспомогательный язык"
        return None

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
        if not mandatory:
            return False
        far_east = any(
            marker in text
            for marker in (
                "дальний восток",
                "владивосток",
                "хабаровск",
                "приморск",
                "хабаровский край",
                "сахалин",
                "камчат",
                "магадан",
                "чукот",
                "якутск",
                "якутия",
                "республика саха",
                "амурская область",
                "благовещенск",
            )
        )
        if far_east:
            return True
        if context.relocation_allowed is not None:
            return not context.relocation_allowed
        return False

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


class AdjacentItRules(PythonBackendRules):
    scope: ClassVar[DirectionScope] = DirectionScope.IT_ADJACENT
    requires_python_without_profile: ClassVar[bool] = False
    _stretch_specializations: ClassVar[tuple[str, ...]] = (
        *PythonBackendRules._stretch_specializations,
        "machine learning",
        "ml engineer",
        "ml-инженер",
        "ml инженер",
        "ml lead",
    )
    _excluded_specializations: ClassVar[tuple[tuple[str, str], ...]] = (
        ("аналитик", "другое направление: аналитика без разработки"),
        ("analyst", "другое направление: аналитика без разработки"),
        ("ручной тестировщик", "работа не связана с написанием кода"),
        ("manual qa", "работа не связана с написанием кода"),
        ("тестирован", "другое направление: проверка качества"),
        ("qa engineer", "другое направление: проверка качества"),
        ("qa-инженер", "другое направление: проверка качества"),
        ("тестировщик", "другое направление: проверка качества"),
        ("mobile", "другое основное направление: мобильная разработка"),
        ("frontend", "другое основное направление: клиентская разработка"),
        ("front-end", "другое основное направление: клиентская разработка"),
        ("фронтенд", "другое основное направление: клиентская разработка"),
        ("embedded", "другое основное направление: встроенные системы"),
        ("встраиваем", "другое основное направление: встроенные системы"),
        ("информационной безопасности", "другое основное направление: безопасность"),
        ("information security", "другое основное направление: безопасность"),
        ("безопасност", "другое основное направление: безопасность"),
        ("security", "другое основное направление: безопасность"),
        ("системный администратор", "другое основное направление: системное администрирование"),
        ("ит-инфраструктур", "другое основное направление: ИТ-инфраструктура"),
        ("серверным платформ", "другое основное направление: ИТ-инфраструктура"),
        ("robotics", "другое основное направление: робототехника"),
        ("робототех", "другое основное направление: робототехника"),
        ("power bi", "другое основное направление: отчётность"),
        ("разработчик sql", "другое основное направление: разработка баз данных"),
        ("sql developer", "другое основное направление: разработка баз данных"),
        ("преподаватель", "другое основное направление: обучение"),
        ("продаж", "работа не связана с разработкой"),
    )

    @staticmethod
    def _role_score(title: str, text: str) -> float:
        if any(marker in title for marker in ("fullstack", "full-stack", "full stack")):
            return 100
        if any(
            marker in title
            for marker in (
                "automation",
                "автоматизац",
                "интеграц",
                "автотест",
                "etl",
                "devops",
                "llm",
                "rag",
                "ai agent",
            )
        ):
            return 90
        if "python" in title:
            return 80
        if "python" in text:
            return 70
        return 55


class VacancyAnalysisService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)
        self._directions = DirectionRepository(session)
        self._vacancies = VacancyRepository(session)
        self._rules = {
            DirectionScope.PYTHON_BACKEND: PythonBackendRules(),
            DirectionScope.IT_ADJACENT: AdjacentItRules(),
        }
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
            result = self._apply(direction, stored, vacancy, context)
            self._route(account.id, direction, stored, vacancy, result)
            results.append(result)
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
            result = self._apply(direction, stored, vacancy, context)
            self._route(account.id, direction, stored, vacancy, result)
            results.append(result)
        return tuple(results)

    def _apply(
        self,
        direction: DirectionRecord,
        stored: VacancyRecord,
        vacancy: VacancyData,
        context: RuleContext,
    ) -> VacancyAnalysisResult:
        tracked = self._directions.get_tracked_vacancy(direction.id, stored.id)
        if tracked.rules_details.get("manual_override") == "ACCEPT":
            raw_reasons = tracked.rules_details.get("reasons", [])
            reason_values = raw_reasons if isinstance(raw_reasons, list) else []
            reasons = tuple(str(item) for item in reason_values)
            evaluation = RuleEvaluation(
                tracked.rules_score or 50,
                RuleCategory.MATCH,
                reasons or ("решение изменено пользователем",),
            )
            state = (
                VacancyState.QUEUED
                if tracked.state is VacancyState.QUEUED
                else VacancyState.ANALYZED
            )
            self._directions.apply_rules(
                direction.id,
                stored.id,
                state=state,
                score=evaluation.score,
                details={
                    **tracked.rules_details,
                    "category": RuleCategory.MATCH.value,
                    "accepted": True,
                },
                rules_version=RULES_VERSION,
            )
            return VacancyAnalysisResult(stored, evaluation, state)

        candidates = self._vacancies.list_duplicate_candidates(stored)
        duplicate = self._duplicates.find(stored, candidates)
        if duplicate is not None:
            stored = self._vacancies.mark_duplicate(
                stored.id,
                duplicate.canonical.id,
                duplicate.similarity,
            )

        rules = self._rules[direction.scope]
        evaluation = rules.evaluate(vacancy, context)
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
                evaluation.target_scope,
            )
        if vacancy.availability is not VacancyAvailability.ACTIVE:
            state = VacancyState.CLOSED
        elif evaluation.category is RuleCategory.ROUTED:
            state = VacancyState.SKIPPED
        elif evaluation.accepted:
            state = (
                VacancyState.QUEUED
                if tracked.state is VacancyState.QUEUED
                else VacancyState.ANALYZED
            )
        else:
            state = VacancyState.FILTERED_OUT

        self._directions.apply_rules(
            direction.id,
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
                "soft_boundary": rules.soft_boundary,
                "duplicate_of_id": stored.duplicate_of_id,
                "target_scope": (
                    evaluation.target_scope.value if evaluation.target_scope is not None else None
                ),
            },
            rules_version=RULES_VERSION,
        )
        return VacancyAnalysisResult(stored, evaluation, state)

    def _route(
        self,
        account_id: int,
        source: DirectionRecord,
        stored: VacancyRecord,
        vacancy: VacancyData,
        result: VacancyAnalysisResult,
    ) -> None:
        target_scope = result.evaluation.target_scope
        if result.evaluation.category is not RuleCategory.ROUTED or target_scope is None:
            return
        target = next(
            (
                direction
                for direction in self._directions.list_for_account(account_id)
                if direction.id != source.id
                and direction.is_active
                and direction.scope is target_scope
            ),
            None,
        )
        if target is None:
            return
        self._directions.track_vacancy(target.id, stored.id)
        target_context = self._context(account_id, target.name)
        self._apply(target, stored, vacancy, target_context)

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
            relocation_allowed=self._relocation_allowed(account_id),
        )

    def _relocation_allowed(self, account_id: int) -> bool | None:
        values = tuple(
            self._session.scalars(
                select(VerifiedFactModel.content)
                .join(
                    CandidateProfileModel,
                    CandidateProfileModel.id == VerifiedFactModel.profile_id,
                )
                .where(
                    CandidateProfileModel.account_id == account_id,
                    VerifiedFactModel.category == "mobility",
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                )
                .order_by(VerifiedFactModel.id.desc())
            )
        )
        content = " ".join(values).casefold()
        if not content:
            return None
        if "не готов к переезду" in content or "переезд не рассматри" in content:
            return False
        if "готов к переезду" in content:
            return True
        return None

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
