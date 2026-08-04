from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancyRecord
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.career_directions import CareerDirectionService
from hugin.services.vacancy_duplicates import VacancyDuplicateDetector

RULES_VERSION = "python_it_v32"
MAX_VACANCY_AGE = timedelta(days=30)


def _normalize_rule_text(value: str | None) -> str:
    if not value:
        return ""
    normalized_spaces = (
        value.replace("\N{NO-BREAK SPACE}", " ")
        .replace("\N{NARROW NO-BREAK SPACE}", " ")
        .replace("\N{FIGURE SPACE}", " ")
    )
    return re.sub(r"\s+", " ", normalized_spaces).strip().casefold()


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
    candidate_locations: tuple[str, ...] = ()
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
    _non_development_title_markers: ClassVar[tuple[str, ...]] = (
        "наставник",
        "преподаватель",
        "педагог",
        "учитель",
        "instructor",
        "trainer",
        "ментор",
        "куратор курса",
    )
    _adjacent_title_markers: ClassVar[tuple[str, ...]] = (
        "fullstack",
        "full-stack",
        "full stack",
        "фулстек",
        "бэк и фронт",
        "бэкенд и фронтенд",
        "backend/frontend",
        "automation",
        "автоматизац",
        "интеграц",
        "integration",
        "автотест",
        "qa automation",
        "test automation",
        "sdet",
        "software development engineer in test",
        "etl",
        "data engineer",
        "data-инженер",
        "data инженер",
        "инженер данных",
        "дата-инженер",
        "дата инженер",
        "devops",
        "sre",
        "platform engineer",
        "mlops",
        "sql-разработчик",
        "sql разработчик",
        "разработчик sql",
        "sql developer",
        "bitrix",
        "битрикс",
        "llm",
        "rag",
        "ai agent",
        "ai engineer",
        "nlp engineer",
        "ml engineer",
        "ml-инженер",
        "ml инженер",
        "ml-разработчик",
        "ml разработчик",
        "ml developer",
        "machine learning developer",
        "ml lead",
        "machine learning",
        "computer vision",
        "компьютерное зрение",
        "компьютерного зрения",
        "техническое зрение",
        "технического зрения",
        "ai developer",
        "ai-разработчик",
        "ai разработчик",
        "ai-инженер",
        "ai инженер",
        "инженер по ai",
        "ии-разработчик",
        "ии разработчик",
        "ии-инженер",
        "ии инженер",
        "инженер по ии",
        "инженер ии",
        "разработчик ии",
        "ai-агент",
        "ai агент",
        "искусственный интеллект",
        "искусственного интеллекта",
        "искусственному интеллекту",
        "искусственным интеллектом",
        "искусственном интеллекте",
        "generative ai",
        "ai/ml",
        "dwh",
        "data warehouse",
        "хранилищ данных",
        "хранилища данных",
    )
    _build_infrastructure_markers: ClassVar[tuple[str, ...]] = (
        "система сборки",
        "системы сборки",
        "систему сборки",
        "ci/cd",
        "ci cd",
        "cmake",
        "makefile",
        "autotools",
        "инфраструктур",
        "build system",
        "build pipeline",
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
        title = _normalize_rule_text(vacancy.title)
        if any(marker in title for marker in cls._non_development_title_markers):
            return None
        complete_text = " ".join(
            (
                title,
                _normalize_rule_text(vacancy.description),
                _normalize_rule_text(vacancy.responsibilities),
                _normalize_rule_text(vacancy.required_qualifications),
                _normalize_rule_text(" ".join(vacancy.key_skills)),
            )
        )
        testing_title = any(
            marker in title for marker in ("тестирован", "тестировщик", "qa engineer", "qa-инженер")
        )
        automated_testing = any(
            marker in complete_text
            for marker in (
                "автоматизац",
                "автоматизирован",
                "автотест",
                "automation",
                "test automation",
                "locust",
                "pytest",
            )
        )
        if testing_title and automated_testing and "python" in complete_text:
            return DirectionScope.IT_ADJACENT
        if cls.is_build_infrastructure_role(title, complete_text):
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
        return None

    @classmethod
    def is_build_infrastructure_role(cls, title: str, complete_text: str) -> bool:
        if "python" in title or any(marker in title for marker in cls._backend_markers):
            return False
        markers_found = sum(marker in complete_text for marker in cls._build_infrastructure_markers)
        return markers_found >= 2


class PythonBackendRules:
    soft_boundary: ClassVar[float] = 50
    scope: ClassVar[DirectionScope] = DirectionScope.PYTHON_BACKEND
    requires_python: ClassVar[bool] = True
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
        "автотест",
        "test automation",
        "pytest",
        "locust",
        "pipeline",
        "пайплайн",
        "скрипт",
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
    _unpaid_compensation_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\bбез\s+зарплат\w*\b|"
        r"\bнеоплачиваем\w*\s+(?:работ|стажиров|занятост|сотрудничеств)\w*\b|"
        r"\b(?:работ|стажиров|занятост|сотрудничеств)\w*\s+неоплачиваем\w*\b|"
        r"\b(?:оплата|вознаграждение)\s+"
        r"(?:только|исключительно)\s+(?:дол\w*|опцион\w*)\b|"
        r"\b(?:дол\w*|опцион\w*)\s+вместо\s+(?:зарплат\w*|оклад\w*)\b"
        r")"
    )
    _negative_candidate_exclusion_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"не\s+(?:откликайтесь|откликаться|рассматриваем|подходят?)|"
        r"просьба\s+не\s+откликаться"
        r")[^.!?\n]{0,180}"
        r"(?:python|fastapi|django|flask|backend|бэкенд|разработчик\w*)"
    )
    _cover_letter_questions_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"сопроводительн\w*\s+письм\w*[^.!?\n]{0,200}"
        r"(?:ответ\w*[^.!?\n]{0,80}вопрос\w*|вопрос\w*)|"
        r"(?:ответ\w*[^.!?\n]{0,80}вопрос\w*|вопрос\w*)[^.!?\n]{0,200}"
        r"сопроводительн\w*\s+письм\w*"
        r")"
    )
    _external_application_form_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:forms\.gle|docs\.google\.com/forms)|"
        r"(?:заполн\w*|пройд\w*)[^.!?\n]{0,100}(?:внешн\w*\s+)?"
        r"(?:форм\w*|анкет\w*)[^.!?\n]{0,100}https?://"
    )
    _support_primary_duties_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:техническ\w*\s+поддержк\w*|"
        r"консультир\w*\s+(?:клиент\w*|пользовател\w*)|"
        r"настройк\w*\s+оборудован\w*)"
    )
    _coding_action_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:разрабатыва\w*|реализовыва\w*|писать\s+код|"
        r"программирова\w*|создава\w*\s+(?:сервис\w*|api|скрипт\w*))"
    )
    _excluded_roles: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            r"\bquant(?:itative)?\s+trader\b|\bквант\w*\s+трейдер\b",
            "основная роль: количественный трейдинг",
        ),
        (
            r"\b(?:junior\s+)?product\s+(?:manager|owner)\b|"
            r"\bпродуктов\w*\s+менеджер\b|\bменеджер\w*\s+продукт",
            "основная роль: управление продуктом",
        ),
        (
            r"\bнаставник\b|\bпреподаватель\b|\bпедагог\b|\bучитель\b|"
            r"\b(?:instructor|trainer)\b|\bментор\b|\bкуратор\w*\s+курс",
            "основная роль: обучение или наставничество",
        ),
        (
            r"\bвайб[ -]?(?:кодер|кодинг)\b|\bvibe[ -]?(?:coder|coding)\b|"
            r"\bno[ -]?code\b|\blow[ -]?code\b",
            "основная роль: no-code/вайбкодинг",
        ),
        (
            r"\b(?:инженер|специалист)(?:\w*|\s+по)\s+сопровождени\w*\b|"
            r"\bинженер\w*\s+эксплуатаци\w*\b|\bsupport engineer\b",
            "основная роль: сопровождение или эксплуатация",
        ),
        (
            r"\bинженер\w*\s+внедрения\b|\bimplementation engineer\b",
            "основная роль: внедрение и сопровождение",
        ),
        (
            r"\bсетев\w*\s+(?:инженер|администратор)\w*\b|"
            r"\bnetwork\s+(?:engineer|administrator)\b",
            "основная роль: сетевое администрирование",
        ),
        (
            r"\b(?:веб[ -]?мастер|web[ -]?master)\b|"
            r"\b(?:cpa[ -]?маркетолог|арбитражник\w*\s+трафик\w*)\b",
            "основная роль: привлечение трафика и интернет-маркетинг",
        ),
        (
            r"\bcustomer\s+journey\s+expert\b|\bcje\b",
            "основная роль: управление клиентским опытом",
        ),
        (
            r"\blinux\s+kernel\b|\bkernel\s+(?:developer|engineer)\b|"
            r"\bразработчик\w*\s+(?:ядр\w*|модул\w*\s+ядр\w*)",
            "основная роль: низкоуровневое системное программирование",
        ),
        (
            r"\bасу\s*тп\b|\bscada\b|"
            r"\b(?:инженер|специалист)\w*\s+по\s+автоматизаци\w*\s+"
            r"(?:производств\w*|технологическ\w*\s+процесс)",
            "основная роль: промышленная автоматизация",
        ),
        (
            r"\bинженер\s*[-–—‑]?\s*технолог\b",
            "основная роль: производственная технология",
        ),
        (
            r"^\s*научн\w*\s+сотрудник\w*\s*$",
            "основная роль: научные исследования",
        ),
        (
            r"^\s*консультант\w*\s*$",
            "основная роль: консультации без подтверждённой разработки",
        ),
    )
    _python_backend_excluded_roles: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            r"\bdevops\b|\bsre\b",
            "основная роль: DevOps/SRE",
        ),
        (
            r"\b(?:qa|aqa)\b|"
            r"\bинженер\w*\s+по\s+тестированию\b|"
            r"\bтестировщик\b|"
            r"\bавтоматизатор\w*\s+тестирован",
            "основная роль: QA/AQA",
        ),
        (
            r"\bappsec\b|\bpentest\b|\bпентест|"
            r"\b(?:инженер|специалист)\w*\s+иб\b|\bdevsecops\b",
            "основная роль: информационная безопасность",
        ),
        (
            r"\bbi[ -]?(?:разработчик|developer|аналитик)\b|"
            r"\b(?:разработчик|developer)\w*\s+bi\b",
            "основная роль: BI",
        ),
        (
            r"\bdata[ -]?scientist\b|\bдата[ -]?саентист\b",
            "основная роль: Data Science",
        ),
        (
            r"\bdba\b|\bdatabase administrator\b|"
            r"\bадминистратор\w*\s+баз\w*\s+данн",
            "основная роль: администрирование баз данных",
        ),
        (
            r"\bпродюсер\b|\bwebinar producer\b",
            "основная роль: продюсирование",
        ),
        (
            r"\bdata[ -]?engineer\b|\bдата[ -]?инженер\b|"
            r"\bинженер\w*\s+данн|\bbig\s*data\b|\bbigdata\b",
            "основная роль: инженерия данных",
        ),
        (
            r"\bтехническ\w*\s+поддержк|"
            r"\bспециалист\w*\s+поддержк|\bsupport engineer\b",
            "основная роль: техническая поддержка",
        ),
        (
            r"\bнаучн\w*\s+сотрудник\b|\bматематик\b",
            "основная роль: научная или математическая работа",
        ),
        (
            r"\bменеджер\w*\s+по\s+продажам\b|\bsales manager\b",
            "основная роль: продажи",
        ),
    )
    _python_backend_excluded_functions: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            r"\bроботизац\w*\s+бизнес[ -]?процесс",
            "основная функция: роботизация бизнес-процессов",
        ),
    )
    _elevated_level_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\b(?:senior|lead|principal)\b|"
        r"\b(?:tech|team)[ -]*lead\b|"
        r"\bлид(?:а|ом|ер\w*)?\b|"
        r"\bmiddle\s*\+|"
        r"\bтех[ -]*лид|"
        r"\bведущ|"
        r"\bстарш|"
        r"\bглавн"
        r")"
    )
    _managerial_level_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\b(?:head|architect)\b|"
        r"\bруководител|"
        r"\bархитектор"
        r")"
    )
    _senior_responsibility_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\b(?:руковод\w+|управлен\w+)\s+(?:команд|разработ)|"
        r"\bруководств\w*\s+команд|"
        r"\bв\s+подчинени\w*\s+\d+\s+(?:разработ|инженер|сотрудник)|"
        r"\bформирован\w+\s+(?:команд|техническ\w+\s+стратег)|"
        r"\bответствен\w+\s+за\s+(?:архитектур|техническ\w+\s+стратег|найм|команд)|"
        r"\b(?:manage|lead)\s+(?:an?\s+)?(?:engineering\s+)?team\b|"
        r"\b(?:people management|technical strategy|hiring)\b"
        r")"
    )
    _negated_senior_responsibility_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\bбез\s+(?:(?:прям\w+|непосредственн\w+)\s+)?(?:"
        r"(?:руковод\w+|управлен\w+)\s+(?:команд\w*|разработ\w*)|"
        r"руководств\w*\s+команд\w*"
        r")|"
        r"\bне\s+предполага\w*\s+"
        r"(?:руковод\w+|управлен\w+)\s+(?:команд\w*|разработ\w*)|"
        r"\bнет\s+ответствен\w+\s+за\s+"
        r"(?:архитектур\w*|техническ\w+\s+стратег\w*|найм\w*|команд\w*)"
        r")"
    )
    _described_level_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\bmiddle\s*[/\\|–—‑-]\s*senior\b|"
        r"\bsenior\s*[/\\|–—‑-]\s*middle\b"
        r")"
    )
    _founding_engineer_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\bfounding\s+(?:(?:ai|llm)\s+)?engineer\b|"
        r"\bперв\w*(?:\s+и\s+единственн\w*)?\s+"
        r"(?:(?:ai|ии|llm)[ -]?)?инженер\w*\b"
        r")"
    )
    _four_plus_experience_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\b4\s*[-–—‑]\s*5\s*(?:лет|года)|"
        r"\b(?:от|не менее|минимум)\s+4(?:-?х)?\s*(?:лет|года)|"
        r"\b4\s*\+\s*(?:лет|года)|"
        r"\b4\s*[-–—‑]\s*5\s*years?\b|"
        r"\b(?:from|at least|minimum)\s+4\s+years?\b|"
        r"\b4\s*\+\s*years?\b"
        r")"
    )
    _mandatory_development_experience_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(?:опыт\w*|experience)\b"
        r"(?=[^.!?]{0,140}\b(?:python|backend|бэкенд|разработ\w*))"
        r"[^.!?]{0,140}"
        r"(?:"
        r"\b(?:от|не менее|минимум)\s+"
        r"(?:2(?:[.,]5)?|[3-9])(?:-?х)?\s*(?:лет|год(?:а|ов)?)\b|"
        r"\b(?:от|не менее|минимум)\s+"
        r"(?:двух|тр[её]х|четыр[её]х|пяти|шести)\s+лет\b|"
        r"\b(?:2(?:[.,]5)?|[3-9])\s*\+\s*(?:лет|год(?:а|ов)?)\b|"
        r"\b(?:from|at least|minimum)\s+(?:2(?:[.,]5)?|[3-9])\+?\s+years?\b|"
        r"\b(?:2(?:[.,]5)?|[3-9])\s*\+\s+years?\b"
        r")"
    )
    _mandatory_english_development_experience_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(?:python|backend|development)\b"
        r"[^.!?]{0,80}\bexperience\b[^.!?]{0,40}"
        r"(?:\b(?:from|at least|minimum)\s+)?"
        r"(?:2(?:[.,]5)?|[3-9])\s*\+?\s+years?\b"
    )
    _mandatory_prefixed_development_experience_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\b(?:от|не менее|минимум)\s+"
        r"(?:2(?:[.,]5)?|[3-9])(?:-?х)?\s*(?:лет|год(?:а|ов)?)\b|"
        r"\b(?:from|at least|minimum)\s+(?:2(?:[.,]5)?|[3-9])\+?\s+years?\b"
        r")"
        r"[^.!?]{0,100}\b(?:опыт\w*|experience)\b"
        r"[^.!?]{0,100}\b(?:python|backend|бэкенд|fullstack|разработ\w*)"
    )
    _optional_python_language_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:"
        r"\bодн\w+\s+из[^.!?]{0,140}"
        r"(?:язык\w*\s+программирован\w*[^.!?]{0,100})?\bpython\b|"
        r"\bкакого-либо\s+язык\w*\s+программирован\w*[^.!?]{0,100}"
        r"\bнапример\s+python\b"
        r")"
    )

    def evaluate(
        self,
        vacancy: VacancyData,
        context: RuleContext | None = None,
    ) -> RuleEvaluation:
        context = context or RuleContext()
        title = _normalize_rule_text(vacancy.title)
        description = _normalize_rule_text(vacancy.description)
        responsibilities = _normalize_rule_text(vacancy.responsibilities)
        requirements = _normalize_rule_text(vacancy.required_qualifications)
        skills = _normalize_rule_text(" ".join(vacancy.key_skills))
        complete_text = " ".join((title, description, responsibilities, requirements, skills))
        experience = self._normalize_experience(vacancy.experience)
        reasons: list[str] = []
        rejected: list[str] = []
        stretch_reasons: list[str] = []
        components: list[RuleComponent] = []

        if vacancy.availability is not VacancyAvailability.ACTIVE:
            rejected.append(f"вакансия недоступна: {vacancy.availability.value}")
        if self._is_too_old(vacancy.published_at):
            rejected.append("вакансия опубликована более 30 дней назад")
        scam = next((marker for marker in self._scam_markers if marker in complete_text), None)
        if scam is not None:
            rejected.append(f"подозрительное требование: {scam}")
        if self._unpaid_compensation_pattern.search(complete_text):
            rejected.append("работа явно не предусматривает денежную оплату")
        if self._negative_candidate_exclusion_pattern.search(" ".join((requirements, description))):
            rejected.append("работодатель прямо исключил кандидатов с текущим профилем разработки")
        if self._cover_letter_questions_pattern.search(description):
            stretch_reasons.append(
                "работодатель требует отдельные ответы в сопроводительном письме; "
                "нужна ручная проверка"
            )
        if self._external_application_form_pattern.search(description):
            stretch_reasons.append("работодатель требует внешнюю форму; нужна ручная проверка")
        if vacancy.has_test_assignment:
            stretch_reasons.append(
                "работодатель указал испытательное задание; нужна ручная проверка"
            )
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
        excluded_roles = self._excluded_roles
        if self.scope is DirectionScope.PYTHON_BACKEND:
            excluded_roles = (*excluded_roles, *self._python_backend_excluded_roles)
        excluded_role = next(
            (reason for pattern, reason in excluded_roles if re.search(pattern, title) is not None),
            None,
        )
        if (
            excluded_role == "основная роль: no-code/вайбкодинг"
            and self._substantial_coding_evidence(
                ". ".join((title, description, responsibilities, requirements, skills))
            )
        ):
            excluded_role = None
            stretch_reasons.append(
                "роль связана с no-code/вайбкодингом, но содержит существенное "
                "программирование; требуется ручная проверка"
            )
        if excluded_role is not None:
            rejected.append(excluded_role)
        if self.scope is DirectionScope.PYTHON_BACKEND:
            excluded_function = next(
                (
                    reason
                    for pattern, reason in self._python_backend_excluded_functions
                    if re.search(pattern, complete_text) is not None
                ),
                None,
            )
            if excluded_function is not None:
                rejected.append(excluded_function)
            if "python" not in title and self._optional_python_language_pattern.search(
                " ".join((requirements, description))
            ):
                rejected.append("Python указан только как один из необязательных языков")
        experience_requirements = self._mandatory_requirements(vacancy)
        if not experience_requirements:
            experience_requirements = self._without_optional_requirements(
                " ".join((requirements, description))
            )
        minimum_required_experience = self._minimum_required_experience(vacancy)
        if minimum_required_experience is not None and minimum_required_experience >= 3:
            stretch_reasons.append("обязательный стаж от трёх лет требует ручной проверки")
        elif (
            hh_experience_minimum := self._experience_minimum(experience)
        ) is not None and hh_experience_minimum >= 3:
            stretch_reasons.append(
                "диапазон опыта hh.ru начинается от трёх лет и требует ручной проверки"
            )
        elif (
            self._mandatory_development_experience_pattern.search(experience_requirements)
            or self._mandatory_english_development_experience_pattern.search(
                experience_requirements
            )
            or self._mandatory_prefixed_development_experience_pattern.search(
                experience_requirements
            )
        ):
            stretch_reasons.append(
                "требование от двух лет опыта снижает приоритет, но само по себе не блокирует"
            )
        level_context = " ".join((title, requirements, description))
        elevated_level = self._elevated_level_pattern.search(level_context) is not None
        senior_responsibilities = " ".join((responsibilities, requirements, description))
        senior_responsibilities = self._negated_senior_responsibility_pattern.sub(
            "",
            senior_responsibilities,
        )
        if self._managerial_level_pattern.search(title):
            rejected.append("руководящая или архитектурная роль выше целевого")
        elif elevated_level and self._senior_responsibility_pattern.search(senior_responsibilities):
            rejected.append("уровень Senior/Lead с обязанностями, существенно выше текущего опыта")
        elif elevated_level:
            stretch_reasons.append(
                "уровень Senior/Lead снижает приоритет, но сам по себе не блокирует"
            )
        if self._founding_engineer_pattern.search(complete_text):
            stretch_reasons.append(
                "роль первого инженера требует ручной проверки масштаба ответственности"
            )
        if self._described_level_pattern.search(" ".join((description, requirements))):
            stretch_reasons.append(
                "уровень Middle/Senior указан как риск, а не самостоятельный запрет"
            )
        if self._more_than_six_years(experience):
            rejected.append("hh.ru указывает требуемый опыт более 6 лет")
        if minimum_required_experience is None and self._four_plus_experience_pattern.search(
            " ".join((requirements, description))
        ):
            stretch_reasons.append(
                "требование от четырёх лет требует дополнительной проверки, "
                "но само по себе не блокирует"
            )

        has_development = any(marker in complete_text for marker in self._development_markers)
        if (
            self.scope is DirectionScope.IT_ADJACENT
            and VacancyRoleRouter.is_build_infrastructure_role(title, complete_text)
        ):
            stretch_reasons.append(
                "основные обязанности связаны со сборкой, CI/CD и инфраструктурой; "
                "требуется ручная проверка"
            )
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
                        "автоматизирован",
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
        described_other_stack = self._described_other_stack(description)
        if described_other_stack is not None and "python" not in title:
            stretch_reasons.append(
                f"основной стек вакансии — {described_other_stack}; требуется ручная проверка"
            )
        if not has_development:
            rejected.append("работа не связана с написанием кода или технической автоматизацией")
        primary_duties = responsibilities or description
        if (
            self._support_primary_duties_pattern.search(primary_duties)
            and self._coding_action_pattern.search(primary_duties) is None
        ):
            rejected.append(
                "основные обязанности связаны с поддержкой и настройкой, а не разработкой"
            )

        profile_tokens = self._profile_skill_tokens(context.skills)
        vacancy_tokens = self._tokens(" ".join((complete_text, skills)))
        skill_overlap = sorted(profile_tokens & vacancy_tokens)
        mandatory_skill_gaps = self._mandatory_skill_gaps(vacancy, profile_tokens)
        if len(mandatory_skill_gaps) >= 2:
            rejected.append(
                "несколько обязательных профильных технологий не подтверждены: "
                + "; ".join(mandatory_skill_gaps)
            )
        elif mandatory_skill_gaps:
            stretch_reasons.append(
                f"{mandatory_skill_gaps[0]}; снижает приоритет и требует ручной проверки"
            )
        if self.requires_python and "python" not in complete_text:
            rejected.append("Python не указан в названии, описании или навыках")
        elif (
            context.skills
            and profile_tokens
            and not skill_overlap
            and self._explicit_other_stack(title, requirements)
        ):
            rejected.append("обязательные технологии не связаны с подтверждённым опытом")

        if self._relocation_conflicts(complete_text, context):
            rejected.append("обязательный переезд противоречит подтверждённым настройкам")
        if self._location_conflicts(vacancy, context):
            rejected.append("офис или гибрид находится вне выбранных регионов")
        if self._salary_below_threshold(vacancy, context):
            rejected.append("верхняя граница зарплаты ниже установленного порога")

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
        specialization_stretch = any(marker in title for marker in self._stretch_specializations)
        if rejected:
            category = RuleCategory.REJECTED
        elif specialization_stretch or stretch_reasons:
            category = RuleCategory.STRETCH
            if specialization_stretch:
                reasons.append(
                    "отдельная специализация; потребуется дополнительная подготовка "
                    "и ручная проверка"
                )
        else:
            category = RuleCategory.MATCH
            if score < self.soft_boundary:
                reasons.append(
                    f"мягкая оценка ниже {self.soft_boundary:.0f}; "
                    "это влияет только на порядок очереди"
                )
        reasons.extend(stretch_reasons)
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
            (r"\bruby\b|\bruby\s+on\s+rails\b|\brails\b", "Ruby/Rails"),
            (r"\babap\b|\bsap\b", "ABAP/SAP"),
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
                r"основн\w+\s+(?:язык|стек).{0,70}\brust\b|"
                r"\brust\b.{0,70}обязател\w*|"
                r"обязател\w*.{0,70}\brust\b)",
                "Rust",
            ),
            (
                r"(?:(?:1[сc])\b.{0,70}основн\w+\s+(?:язык|стек)|"
                r"основн\w+\s+(?:язык|стек).{0,70}(?:1[сc])\b)",
                "1С",
            ),
            (
                r"(?:скриптов\w*\s+язык\w*.{0,50}\bpawn\b|"
                r"\bpawn\b.{0,70}серверн\w+\s+сторон)",
                "Pawn",
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
                r"втор\w*\s+язык|границ\w*\s+(?:ml|ai)|"
                r"пример\w*\s+базов\w+\s+язык)",
                text,
            )
            is not None
        ):
            return "Python указан только как вспомогательный язык"
        return None

    @staticmethod
    def _described_other_stack(description: str) -> str | None:
        patterns = (
            (
                r"(?:наш|основн\w*|технологическ\w*)\s+стек\s*:?"
                r"[^.!?\n]{0,100}\b(?:ruby|rails)\b",
                "Ruby/Rails",
            ),
        )
        return next(
            (label for pattern, label in patterns if re.search(pattern, description) is not None),
            None,
        )

    @classmethod
    def _mandatory_skill_gaps(
        cls,
        vacancy: VacancyData,
        profile_tokens: set[str],
    ) -> tuple[str, ...]:
        if not profile_tokens:
            return ()
        requirements = cls._mandatory_requirements(vacancy)
        if not requirements:
            return ()

        gaps: list[str] = []
        title = _normalize_rule_text(vacancy.title)
        django_required = (
            "django" in title
            or re.search(
                r"(?:опыт\w*\s+работ\w*\s+с\s+django|"
                r"опыт\w*[^.!?]{0,80}\bdjango\b|"
                r"python\w*\s+и\s+django|"
                r"приоритетн\w*\s+опыт\w*[^.!?]{0,50}\bdjango\b)",
                requirements,
            )
            is not None
        )
        if django_required and "django" not in profile_tokens:
            gaps.append("обязательный Django не подтверждён опытом кандидата")

        broker_required = re.search(
            r"(?:(?:опыт\w*|знан\w*|уверенн\w*|знаком\w*)[^.!?]{0,100}"
            r"(?:rabbitmq|kafka|брокер\w*\s+сообщен)|"
            r"(?:rabbitmq|kafka|брокер\w*\s+сообщен)[^.!?]{0,100}"
            r"(?:опыт\w*|знан\w*|уверенн\w*|знаком\w*))",
            requirements,
        )
        if broker_required is not None and not profile_tokens & {"rabbitmq", "kafka"}:
            gaps.append("обязательный брокер сообщений не подтверждён опытом кандидата")

        frontend_markers = {
            marker
            for marker in (
                "javascript",
                "typescript",
                "react",
                "vue",
                "nuxt",
                "jquery",
                "bootstrap",
                "html5",
                "css",
                "tailwind",
            )
            if marker in requirements
        }
        if len(frontend_markers) >= 2 and not profile_tokens & frontend_markers:
            gaps.append("обязательный клиентский стек не подтверждён опытом кандидата")
        gaps.extend(cls._mandatory_specialist_skill_gaps(requirements, profile_tokens))
        return tuple(dict.fromkeys(gaps))

    @staticmethod
    def _mandatory_specialist_skill_gaps(
        requirements: str,
        profile_tokens: set[str],
    ) -> tuple[str, ...]:
        specialist_requirements = (
            (
                "Apache Spark",
                r"\b(?:apache\s+)?spark(?:\s+sql)?\b|\bpyspark\b",
                frozenset({"spark", "pyspark"}),
            ),
            (
                "Hadoop",
                r"\b(?:apache\s+)?hadoop\b",
                frozenset({"hadoop"}),
            ),
            (
                "Greenplum",
                r"\bgreenplum\b",
                frozenset({"greenplum"}),
            ),
            (
                "CDC",
                r"\bcdc\b|change[ -]data[ -]capture|"
                r"инкрементальн\w*\s+загруз\w*\s+данн\w*",
                frozenset({"cdc", "debezium"}),
            ),
            (
                "ПЛК",
                r"\bплк\b|\bplc\b|мэк\s*61131|iec\s*61131",
                frozenset({"плк", "plc", "iec61131"}),
            ),
            (
                "SCADA/HMI",
                r"\bscada\b|\bhmi\b|\bскада\b",
                frozenset({"scada", "hmi"}),
            ),
            (
                "промышленные протоколы",
                r"\bmodbus\b|\bknx\b|\bdali\b|\bprofibus\b|\bprofinet\b",
                frozenset({"modbus", "knx", "dali", "profibus", "profinet"}),
            ),
            (
                "PyTorch",
                r"\bpytorch\b|\btorch\b",
                frozenset({"pytorch", "torch"}),
            ),
            (
                "компьютерное зрение",
                r"\bopencv\b|\bcomputer\s+vision\b|"
                r"\bкомпьютерн\w*\s+зрени\w*\b|\bтехническ\w*\s+зрени\w*\b",
                frozenset({"opencv", "computer-vision", "cv"}),
            ),
            (
                "ROS",
                r"\bros2?\b|\brobot\s+operating\s+system\b",
                frozenset({"ros", "ros2"}),
            ),
            (
                "C++",
                r"(?<!\w)c\+\+(?:11|14|17|20|23)?(?!\w)",
                frozenset({"c++", "cpp"}),
            ),
            (
                "Qt",
                r"\bqt(?:5|6)?\b|\bqml\b",
                frozenset({"qt", "qt5", "qt6", "qml"}),
            ),
            (
                "цифровая обработка сигналов",
                r"\bdsp\b|цифров\w*\s+обработк\w*\s+сигнал\w*",
                frozenset({"dsp", "signal-processing"}),
            ),
            (
                "макросы Office",
                r"(?:\bexcel\b|\boffice\b)[^.!?\n]{0,100}"
                r"(?:макрос\w*|\bvba\b)|(?:макрос\w*|\bvba\b)[^.!?\n]{0,100}"
                r"(?:\bexcel\b|\boffice\b)",
                frozenset({"vba", "excel-macros"}),
            ),
            (
                "BPMN",
                r"\bbpmn\b",
                frozenset({"bpmn"}),
            ),
            (
                "сетевые основы TCP/IP и OSI",
                r"\btcp\s*/?\s*ip\b|\bмодел\w*\s+osi\b|"
                r"\bсетев\w*\s+протокол\w*\b",
                frozenset({"tcp/ip", "tcpip", "osi", "networking"}),
            ),
            (
                "Scrapy",
                r"\bscrapy\b",
                frozenset({"scrapy"}),
            ),
            (
                "TensorFlow/Keras",
                r"\btensorflow\b|\bkeras\b",
                frozenset({"tensorflow", "keras"}),
            ),
            (
                "SIEM",
                r"\bsiem\b|систем\w*\s+управлен\w*\s+событи\w*\s+"
                r"информационн\w*\s+безопасност\w*",
                frozenset({"siem"}),
            ),
            (
                "Elasticsearch/ELK",
                r"\belasticsearch\b|\belk\b",
                frozenset({"elasticsearch", "elk"}),
            ),
            (
                "Pandas",
                r"\bpandas\b",
                frozenset({"pandas"}),
            ),
            (
                "DWH",
                r"\bdwh\b|хранилищ\w*\s+данн\w*",
                frozenset({"dwh", "data-warehouse"}),
            ),
            (
                "Airflow",
                r"\bairflow\b",
                frozenset({"airflow"}),
            ),
            (
                "Kubernetes",
                r"\bkubernetes\b|\bk8s\b",
                frozenset({"kubernetes", "k8s"}),
            ),
            (
                "Go",
                r"\bgolang\b|\bgo\b",
                frozenset({"go", "golang"}),
            ),
            (
                "Node.js",
                r"\bnode(?:\.js)?\b|\bnodejs\b",
                frozenset({"node.js", "nodejs"}),
            ),
            (
                "микросервисная архитектура",
                r"\bмикросервис\w*\s+архитектур\w*\b|"
                r"\bmicroservice\w*\s+architecture\b",
                frozenset({"микросервисы", "microservices", "microservice"}),
            ),
            (
                "RAG",
                r"\brag\b|retrieval[ -]augmented generation|"
                r"генерац\w*\s+с\s+поиск\w*\s+контекст",
                frozenset({"rag"}),
            ),
            (
                "pgvector",
                r"\bpgvector\b",
                frozenset({"pgvector"}),
            ),
            (
                "паттерн ReAct",
                r"(?:\brag\b|\bpgvector\b|\bagent\w*\b|\bагент\w*\b)"
                r"[^.!?]{0,100}\breact\b|"
                r"\breact\b[^.!?]{0,100}(?:\bagent\w*\b|\bагент\w*\b)",
                frozenset({"react", "langgraph", "langchain"}),
            ),
            (
                "LangGraph",
                r"\blanggraph\b",
                frozenset({"langgraph"}),
            ),
            (
                "LangChain",
                r"\blangchain\b",
                frozenset({"langchain"}),
            ),
            (
                "LlamaIndex",
                r"\bllama[ -]?index\b",
                frozenset({"llamaindex", "llama-index"}),
            ),
            (
                "PydanticAI",
                r"\bpydantic[ -]?ai\b",
                frozenset({"pydanticai", "pydantic-ai"}),
            ),
            (
                "CrewAI",
                r"\bcrew[ -]?ai\b",
                frozenset({"crewai", "crew-ai"}),
            ),
            (
                "AutoGen",
                r"\bautogen\b",
                frozenset({"autogen"}),
            ),
            (
                "MCP",
                r"\bmcp\b[^.!?]{0,80}(?:\bagent\w*\b|\bllm\b)|"
                r"(?:\bagent\w*\b|\bllm\b)[^.!?]{0,80}\bmcp\b",
                frozenset({"mcp"}),
            ),
            (
                "векторная база данных",
                r"\bvector\s+(?:database|db|store)\b|"
                r"\bвекторн\w*\s+(?:баз\w*\s+данн\w*|хранилищ\w*)",
                frozenset(
                    {
                        "pgvector",
                        "qdrant",
                        "milvus",
                        "weaviate",
                        "pinecone",
                        "faiss",
                        "chroma",
                        "chromadb",
                    }
                ),
            ),
            (
                "архитектура ИИ-агентов",
                r"\b(?:agent(?:ic)?\s+architecture|architecture\s+of\s+(?:ai\s+)?agents?)\b|"
                r"\bархитектур\w*[^.!?]{0,80}(?:ии|ai|llm)?[ -]?агент\w*\b",
                frozenset(
                    {
                        "langgraph",
                        "langchain",
                        "crewai",
                        "autogen",
                        "pydanticai",
                        "agentic",
                    }
                ),
            ),
        )
        return tuple(
            f"обязательный {label} не подтверждён опытом кандидата"
            for label, pattern, aliases in specialist_requirements
            if re.search(pattern, requirements) is not None and not profile_tokens & aliases
        )

    @classmethod
    def _mandatory_requirements(cls, vacancy: VacancyData) -> str:
        if vacancy.required_qualifications and vacancy.required_qualifications.strip():
            return cls._without_optional_requirements(vacancy.required_qualifications)
        description = vacancy.description or ""
        heading_expression = (
            r"(?:"
            r"требования|"
            r"что\s+мы\s+жд[её]м[^:\n]*|"
            r"что\s+мы\s+ожидаем|"
            r"жд[её]м\s+от\s+тебя|"
            r"мы\s+ожидаем\s+от\s+тебя|"
            r"мы\s+жд[её]м\s+от\s+вас|"
            r"что\s+ожидаем\s+от\s+кандидата|"
            r"будем\s+рады\s+видеть[^:\n]*|"
            r"для\s+нас\s+важно|"
            r"чего\s+мы\s+ожидаем|"
            r"ожидания|"
            r"наши\s+ожидания|"
            r"наш[и]\s+пожелания\s+к\s+кандидатам|"
            r"опыт\s+и\s+навыки|"
            r"кого\s+мы\s+ищем|"
            r"что\s+для\s+этого\s+необходимо|"
            r"технические\s+требования|"
            r"какой\s+опыт\s+и\s+знания\s+нужны|"
            r"что\s+нужно\s+уметь|"
            r"стек\s*\(?(?:обязательно|обязательный)\)?|"
            r"(?:тот|та|кандидат)[^:\n]{0,160}\bимеет"
            r")"
        )
        line_heading = re.search(
            rf"(?im)^\s*{heading_expression}\s*:?\s*$",
            description,
        )
        inline_heading = re.search(
            rf"(?i)(?<!\w){heading_expression}\s*:\s*",
            description,
        )
        headings = tuple(match for match in (line_heading, inline_heading) if match is not None)
        heading = min(headings, key=lambda match: match.start()) if headings else None
        if heading is None:
            return ""
        requirements = description[heading.end() :]
        return cls._without_optional_requirements(requirements)

    @staticmethod
    def _without_optional_requirements(value: str) -> str:
        optional_section = (
            r"(?:"
            r"будет\s+(?:плюсом|преимуществом)|"
            r"желательн\w*\s+навык\w*(?:\s*\(будет\s+плюсом\))?|"
            r"желательно|"
            r"необязательно|"
            r"приветствуется|"
            r"optional|"
            r"preferred|"
            r"nice\s+to\s+have|"
            r"условия|"
            r"мы\s+предлагаем|"
            r"что\s+мы\s+предлагаем|"
            r"предлагаем"
            r")"
        )
        ending = re.search(
            rf"(?im)^\s*{optional_section}\s*:?\s*$",
            value,
        )
        inline_ending = re.search(
            rf"(?i)(?<!\w){optional_section}\s*:\s*",
            value,
        )
        endings = tuple(match for match in (ending, inline_ending) if match is not None)
        if endings:
            value = value[: min(endings, key=lambda match: match.start()).start()]
        lines = value.splitlines()
        if len(lines) > 1:
            optional_clause = re.compile(
                r"\b(?:будет\s+(?:плюсом|преимуществом)|"
                r"желательно|необязательно|приветствуется)\b",
                re.IGNORECASE,
            )
            value = "\n".join(line for line in lines if not optional_clause.search(line))
        return _normalize_rule_text(value)

    @staticmethod
    def _substantial_coding_evidence(text: str) -> bool:
        if re.search(r"\b(?:писать\s+код|write\s+(?:the\s+)?code)\b", text):
            return True
        sections = re.split(
            r"(?:[.!?;]+|\s+[—–•●▪]\s+|\s+-\s+)",
            text,
        )
        stack_markers = (
            "python",
            "backend",
            "api",
            "fastapi",
            "django",
            "postgresql",
            "pytest",
        )
        return any(
            re.search(r"\b(?:разрабатыв\w*|develop\w*)\b", section) is not None
            and any(marker in section for marker in stack_markers)
            for section in sections
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token
            for token in re.findall(
                r"[a-zа-яё][a-zа-яё0-9+#.-]{1,}",
                _normalize_rule_text(text),
            )
            if len(token) > 1
        }

    def _profile_skill_tokens(self, values: tuple[str, ...]) -> set[str]:
        return self._tokens(" ".join(values))

    @classmethod
    def _minimum_required_experience(
        cls,
        vacancy: VacancyData,
    ) -> float | None:
        values: list[float] = []
        mandatory_requirements = cls._mandatory_requirements(vacancy)
        if not mandatory_requirements:
            mandatory_requirements = cls._without_optional_requirements(
                " ".join(
                    (
                        vacancy.required_qualifications or "",
                        vacancy.description or "",
                    )
                )
            )
        for clause in re.split(r"[.!?;\n]+", mandatory_requirements):
            has_experience_marker = (
                re.search(r"\b(?:опыт\w*|стаж\w*|experience)\b", clause) is not None
            )
            has_development_duration = (
                re.search(r"\b(?:лет|год(?:а|ов)?|years?)\b", clause) is not None
                and re.search(
                    r"\b(?:разработ\w*|development|python|backend|бэкенд|"
                    r"автоматизац\w*|интеграц\w*|etl|rpa|n8n)\b",
                    clause,
                )
                is not None
            )
            if not clause or not (has_experience_marker or has_development_duration):
                continue
            minimum = cls._experience_minimum(clause)
            if minimum is not None:
                values.append(minimum)
        return max(values) if values else None

    @staticmethod
    def _experience_minimum(value: str) -> float | None:
        if not value or "не требуется" in value or "без опыта" in value:
            return None
        word_values = {
            "двух": 2.0,
            "трех": 3.0,
            "трёх": 3.0,
            "четырех": 4.0,
            "четырёх": 4.0,
            "пяти": 5.0,
            "шести": 6.0,
        }
        word_match = re.search(
            r"\b(?:от|не менее|минимум)\s+"
            r"(двух|тр[её]х|четыр[её]х|пяти|шести)\s+лет\b",
            value,
        )
        if word_match is not None:
            return word_values[word_match.group(1)]

        patterns = (
            r"\b(?:от|from)\s+(\d+(?:[.,]\d+)?)\s+"
            r"(?:до|to)\s+\d+(?:[.,]\d+)?\s*(?:лет|год(?:а|ов)?|years?)\b",
            r"\b(?:от|не менее|минимум|from|at least|minimum)\s+"
            r"(\d+(?:[.,]\d+)?)(?:-?х)?\s*(?:лет|год(?:а|ов)?|years?)\b",
            r"\b(\d+(?:[.,]\d+)?)\s*\+\s*(?:лет|год(?:а|ов)?|years?)\b",
            r"\b(\d+(?:[.,]\d+)?)\s*[-–—‑]\s*\d+(?:[.,]\d+)?\s*"
            r"(?:лет|год(?:а|ов)?|years?)\b",
            r"\b(\d+(?:[.,]\d+)?)(?:-?х)?\s*(?:лет|год(?:а|ов)?|years?)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, value)
            if match is not None:
                return float(match.group(1).replace(",", "."))
        return None

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
    def _normalize_experience(value: str | None) -> str:
        normalized = (
            _normalize_rule_text(value)
            .replace("\N{EN DASH}", "-")
            .replace("\N{EM DASH}", "-")
            .replace("\N{NON-BREAKING HYPHEN}", "-")
        )
        return re.sub(r"\s*-\s*", "-", normalized)

    @staticmethod
    def _more_than_six_years(experience: str) -> bool:
        return re.search(r"\b(?:более|свыше)\s+6\s+лет\b", experience) is not None

    @staticmethod
    def _experience_score(experience: str) -> float | None:
        if not experience:
            return None
        if "не требуется" in experience:
            return 100
        if "1-3" in experience or re.search(r"\bот 1(?: года)? до 3 лет\b", experience):
            return 90
        if "3-6" in experience or "от 3" in experience:
            return 65
        return 55

    @staticmethod
    def _work_format_score(vacancy: VacancyData, context: RuleContext) -> float | None:
        if not context.work_formats or not vacancy.work_format:
            return None
        value = _normalize_rule_text(vacancy.work_format)
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
        text = _normalize_rule_text(text)
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
    def _location_conflicts(vacancy: VacancyData, context: RuleContext) -> bool:
        if not context.regions or not vacancy.region:
            return False
        work_format = _normalize_rule_text(vacancy.work_format)
        vacancy_text = _normalize_rule_text(
            " ".join(
                filter(
                    None,
                    (
                        vacancy.description,
                        vacancy.responsibilities,
                        vacancy.required_qualifications,
                    ),
                )
            )
        )
        description_requires_office = re.search(
            r"(?:"
            r"\bформат работы\s*:\s*(?:только\s+)?(?:в\s+)?офис(?:е|ный)?\b|"
            r"\b(?:только\s+)?работа\s+в\s+офисе\b"
            r")",
            vacancy_text,
        )
        if (
            "удал" in work_format or "remote" in work_format
        ) and description_requires_office is None:
            return False
        requires_presence = any(
            marker in work_format
            for marker in (
                "на месте работодателя",
                "офис",
                "гибрид",
                "hybrid",
                "on-site",
                "onsite",
            )
        )
        requires_presence = requires_presence or description_requires_office is not None
        if not requires_presence:
            return False
        vacancy_region = _normalize_rule_text(vacancy.region)
        selected_regions = tuple(_normalize_rule_text(region.name) for region in context.regions)
        return not any(
            region and (region in vacancy_region or vacancy_region in region)
            for region in selected_regions
        )

    @staticmethod
    def _salary_below_threshold(vacancy: VacancyData, context: RuleContext) -> bool:
        threshold = context.minimum_salary
        if (
            threshold is None
            or vacancy.salary_to is None
            or vacancy.salary_currency not in {None, "RUR", "RUB"}
        ):
            return False
        return vacancy.salary_to < threshold

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
        work_format = _normalize_rule_text(vacancy.work_format)
        if "удал" in work_format or "remote" in work_format:
            return 100
        vacancy_region = _normalize_rule_text(vacancy.region)
        return (
            100
            if any(
                _normalize_rule_text(region.name) in vacancy_region for region in context.regions
            )
            else 20
        )

    @staticmethod
    def _freshness_score(published_at: datetime | None) -> float | None:
        if published_at is None:
            return None
        age_seconds = (datetime.now(UTC) - as_utc(published_at)).total_seconds()
        age_days = max(age_seconds / 86400, 0)
        if age_days <= 2:
            return 100
        if age_days <= 7:
            return 80
        if age_days <= 30:
            return 55
        return 30

    @staticmethod
    def _is_too_old(published_at: datetime | None) -> bool:
        if published_at is None:
            return False
        return datetime.now(UTC) - as_utc(published_at) > MAX_VACANCY_AGE

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
    requires_python: ClassVar[bool] = False
    _stretch_specializations: ClassVar[tuple[str, ...]] = (
        *PythonBackendRules._stretch_specializations,
        "machine learning",
        "ml engineer",
        "ml-инженер",
        "ml инженер",
        "ml-разработчик",
        "ml разработчик",
        "ml developer",
        "machine learning developer",
        "ml lead",
        "mlops",
        "devops",
        "sre",
        "sdet",
        "software development engineer in test",
        "aqa",
        "тестировщик",
        "qa engineer",
        "qa-инженер",
        "автоматизированн",
        "автотест",
        "pyqt",
        "qml",
        "ai developer",
        "ai-разработчик",
        "ai разработчик",
        "ai-инженер",
        "ai инженер",
        "инженер по ai",
        "ии-разработчик",
        "ии разработчик",
        "ии-инженер",
        "ии инженер",
        "инженер по ии",
        "инженер ии",
        "разработчик ии",
        "ai-агент",
        "ai агент",
        "искусственный интеллект",
        "искусственного интеллекта",
        "искусственному интеллекту",
        "искусственным интеллектом",
        "искусственном интеллекте",
        "generative ai",
        "ai/ml",
        "dwh",
        "data warehouse",
        "хранилищ данных",
        "хранилища данных",
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
        ("sql-разработчик", "другое основное направление: разработка баз данных"),
        ("sql разработчик", "другое основное направление: разработка баз данных"),
        ("sql developer", "другое основное направление: разработка баз данных"),
        ("bitrix", "другой основной стек: Bitrix"),
        ("битрикс", "другой основной стек: Битрикс"),
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
        rules = self._rules[direction.scope]
        if (
            vacancy.availability is VacancyAvailability.ACTIVE
            and tracked.rules_details.get("manual_override") == "ACCEPT"
            and not rules._is_too_old(vacancy.published_at)
        ):
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
            candidate_locations=self._candidate_locations(account_id),
            minimum_salary=settings.minimum_salary,
            desired_salary=settings.desired_salary,
            relocation_allowed=self._relocation_allowed(account_id),
        )

    def _candidate_locations(self, account_id: int) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(VerifiedFactModel.content)
                .join(
                    CandidateProfileModel,
                    CandidateProfileModel.id == VerifiedFactModel.profile_id,
                )
                .where(
                    CandidateProfileModel.account_id == account_id,
                    VerifiedFactModel.category == "location",
                    VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                )
                .order_by(VerifiedFactModel.id.desc())
            )
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
