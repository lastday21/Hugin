from __future__ import annotations

import re
from dataclasses import dataclass

from hugin.domain.directions import DirectionScope
from hugin.domain.vacancies import VacancyData
from hugin.domain.vacancy_priority import FitTier
from hugin.services.requirement_sections import (
    mandatory_clauses,
    mandatory_text,
    primary_duties,
    primary_requirement_clauses,
    primary_requirements,
)
from hugin.services.vacancy_duties import TechnicalDuty, technical_duty_evidence

_CAPABILITIES = {
    "python": r"\bpython\b",
    "web": r"\b(?:fastapi|django|flask|rest|api|http)\b",
    "database": r"\b(?:postgresql|postgres|sql|sqlalchemy)\b",
    "llm": r"\b(?:llm|yandexgpt|gpt|openai|rag|langchain)\b|языков\w* модел",
    "testing": r"\b(?:pytest|playwright|selenium)\b|автотест",
    "deployment": r"\b(?:docker|linux|ci/cd)\b",
    "analytics": r"\b(?:pandas|numpy|excel)\b|анализ данных",
}
_OTHER_ROLE = re.compile(
    r"devops|\bsre\b|администратор|инфраструктур|поддержк|support|"
    r"бизнес.аналитик|системн\w* аналитик|business analyst|system analyst|"
    r"ручн\w*(?:\s+\w+){0,2}\s+тест|manual qa|сетев\w* инженер|дежурн|"
    r"linux[- ]инженер|linux engineer",
    re.I,
)
_RELATED_ROLE = re.compile(
    r"тест|\bqa\b|\baqa\b|\bsdet\b|test|data engineer|инженер данных|"
    r"\betl\b|\bdwh\b|аналитик|analyst|full.?stack|фулстек|desktop|\bqt\b|pyqt",
    re.I,
)
_APPLIED_ROLE = re.compile(
    r"\b(?:ai|llm|rag|ии)\b|автоматизац|automation|интеграц|integration|"
    r"внутренн\w* (?:сервис|прилож)|искусственн",
    re.I,
)
_DEVELOPMENT_DUTIES = re.compile(
    r"разраб[ао]т|создава|реализ|интегр|автоматиз|develop|implement|build|integrat|automat",
    re.I,
)
_INFRASTRUCTURE_ROLE = re.compile(
    r"\b(?:devops|devsecops|mlops|dataops|secops|sre)\b|девопс|"
    r"site reliability engineer|platform engineer|infrastructure engineer|инфраструктур|"
    r"администратор|administrator|linux[- ]инженер|linux engineer|системн\w* инженер",
    re.I,
)
_SECURITY_ROLE = re.compile(r"безопасност|\bsecurity\b|\bскзи\b|\bиб\b", re.I)
_CLUSTER = re.compile(r"\b(?:kubernetes|k8s)\b", re.I)
_GPU_SERVING = re.compile(r"\b(?:triton|vllm|tensorrt(?:[-‑ ]llm)?)\b", re.I)
_CRYPTO_TOOLS = re.compile(r"\b(?:скзи|крипто\s?про|crypto\s?pro)\b", re.I)
_INFRASTRUCTURE_CODE = re.compile(r"\bterraform\b", re.I)
_PRACTICE = re.compile(
    r"\b(?:практическ\w*|коммерческ\w*)[^.;\n]{0,60}"
    r"\b(?:опыт\w*|навык\w*|владени\w*)\b|"
    r"\b(?:опыт|умение)\s+(?:работ\w*|использован\w*|администр\w*)\b",
    re.I,
)
_OPERATING_ACTION = re.compile(
    r"администр\w*|эксплуат\w*|разв[её]ртыва\w*|управл\w*|"
    r"\b(?:operate|administer|deploy|manage)\w*\b",
    re.I,
)
_AWARENESS = re.compile(
    r"знакомств\w*|(?:базов\w*|теоретическ\w*)\s+(?:понимани\w*|знани\w*)|"
    r"на\s+уровне\s+(?:термин\w*|разработчик\w*)|"
    r"(?:понимани\w*|знани\w*)\s+(?:основ|принцип\w*|назначени\w*)",
    re.I,
)
_OTHER_OPERATOR = re.compile(
    r"\b(?:у|силами)\s+(?:(?:друг\w*|отдельн\w*|специализированн\w*)\s+)?"
    r"(?:команд\w*|коллег\w*|подрядчик\w*)|"
    r"\b(?:друг\w*|отдельн\w*|специализированн\w*)\s+команд\w*|"
    r"\b(?:коллег\w*|подрядчик\w*)\s+(?:отвеча\w*|занима\w*|выполня\w*)|"
    r"\bвзаимодейств\w*\s+с\s+(?:команд\w*|коллег\w*)",
    re.I,
)
_REQUIREMENT_DETAIL = re.compile(
    r",\s*(?=(?:базов\w*|теоретическ\w*|знакомств\w*|кандидат\w*|от\s+кандидат\w*)\b)",
    re.I,
)
_HANDOVER = re.compile(r"переда\w*\s+(?:вам|тебе|кандидат\w*)\b", re.I)
_TOOL_ALIASES = (
    r"kubernetes|k8s",
    r"openshift",
    r"terraform",
    r"pulumi",
    r"crypto\s?pro|крипто\s?про",
    r"vipnet|випнет",
    r"triton",
    r"vllm",
    r"tensorrt(?:[-‑ ]llm)?",
)
_TOOL_NAME = rf"(?:{'|'.join(_TOOL_ALIASES)})"
_TOOL_ALTERNATIVES = re.compile(rf"\b{_TOOL_NAME}(?:\s+(?:или|or)\s+{_TOOL_NAME})+\b", re.I)


def _confirmed_or_alternative(clause: str, pattern: re.Pattern[str], profile: str) -> bool:
    if pattern.search(profile):
        return True
    for choice in _TOOL_ALTERNATIVES.finditer(clause):
        if pattern.search(choice.group()) and any(
            re.search(rf"\b(?:{alias})\b", choice.group(), re.I)
            and re.search(rf"\b(?:{alias})\b", profile, re.I)
            for alias in _TOOL_ALIASES
        ):
            return True
    return False


def _operating_requirements(clauses: tuple[str, ...], source: str) -> tuple[str, ...]:
    result: list[str] = []
    source = re.sub(r"\s+", " ", source).casefold()
    for clause in clauses:
        for part in _REQUIREMENT_DETAIL.split(clause):
            if (_OTHER_OPERATOR.search(part) and not _HANDOVER.search(part)) or _AWARENESS.search(
                part
            ):
                continue
            # Отсылка должна указывать на одно явно названное средство.
            if re.search(
                r"\b(?:его|её|ее|их)\s+(?:эксплуат\w*|администр\w*)|"
                r"\b(?:эксплуат\w*|администр\w*)\s+кластер\w*\b",
                part,
            ):
                index = source.find(part)
                previous = re.split(r"[.!?;]|,\s*", source[:index].rstrip(" .;,"))[-1]
                families = (_CLUSTER, _GPU_SERVING, _CRYPTO_TOOLS, _INFRASTRUCTURE_CODE)
                names = [
                    match.group() for pattern in families for match in pattern.finditer(previous)
                ]
                if index >= 0 and len(names) == 1:
                    part = names[0] + ": " + part
            result.append(part)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class FitAssessment:
    tier: FitTier
    reason: str
    matched_capabilities: tuple[str, ...]


def assess_fit(
    vacancy: VacancyData,
    *,
    scope: DirectionScope,
    confirmed_skills: tuple[str, ...],
    mandatory_gaps: tuple[str, ...],
    minimum_years: float | None,
) -> FitAssessment:
    duties = mandatory_text(
        primary_duties(vacancy.description, vacancy.responsibilities), include_unlabelled=True
    )
    requirements = primary_requirements(
        vacancy.description or vacancy.responsibilities, vacancy.required_qualifications
    )
    primary_text = " ".join((duties, requirements))
    profile_text = " ".join(confirmed_skills)
    overlap = tuple(
        name
        for name, pattern in _CAPABILITIES.items()
        if re.search(pattern, profile_text, re.I) and re.search(pattern, primary_text, re.I)
    )

    def result(tier: FitTier, reason: str) -> FitAssessment:
        return FitAssessment(tier, reason, overlap)

    if not confirmed_skills:
        return result(FitTier.POSSIBLE, "подтверждённые навыки профиля пока не загружены")
    if len(mandatory_gaps) >= 2:
        return result(FitTier.POSSIBLE, "не подтверждены несколько обязательных технологий")
    if minimum_years is not None and minimum_years >= 4:
        return result(FitTier.POSSIBLE, "требуемый стаж заметно выше текущего уровня")
    if _OTHER_ROLE.search(vacancy.title):
        return result(FitTier.POSSIBLE, "основная профессия отличается от целевой разработки")
    if not overlap:
        return result(FitTier.POSSIBLE, "в основных задачах нет явного совпадения навыков")
    if _RELATED_ROLE.search(vacancy.title):
        return result(FitTier.RELATED, "навыки применимы в смежной специализации")
    is_applied = _APPLIED_ROLE.search(vacancy.title) is not None
    is_target = (
        scope is DirectionScope.PYTHON_BACKEND
        or is_applied
        or any(item.kind is TechnicalDuty.DEVELOPMENT for item in technical_duty_evidence(vacancy))
    )
    if not is_target:
        return result(FitTier.POSSIBLE, "ИТ-роль допустима, но прямое соответствие не подтверждено")
    mixed_level = re.search(r"middle|мидл|средн", vacancy.title, re.I)
    if not mixed_level and re.search(
        r"стаж[её]р|стажиров|intern|senior|\blead\b|старший|ведущий", vacancy.title, re.I
    ):
        return result(FitTier.RELATED, "задачи близки, но заявленный уровень роли отличается")
    if mandatory_gaps:
        return result(FitTier.RELATED, "есть неподтверждённая обязательная технология")
    if minimum_years is not None and minimum_years >= 3:
        return result(FitTier.RELATED, "основные навыки подходят, требуемый стаж выше текущего")
    is_ai = re.search(r"\b(?:ai|llm|rag|ии)\b|искусственн", vacancy.title, re.I) is not None
    if is_ai and "llm" not in overlap:
        return result(FitTier.RELATED, "прикладной опыт с языковыми моделями не подтверждён")
    if "python" not in overlap or len(overlap) < 2 or not _DEVELOPMENT_DUTIES.search(duties):
        return result(FitTier.RELATED, "для прямого соответствия недостаточно сведений о задачах")
    return result(FitTier.DIRECT, "основные задачи разработки совпадают с подтверждёнными навыками")


def unsupported_administration(
    vacancy: VacancyData, required_text: str, confirmed_skills: tuple[str, ...]
) -> tuple[str, ...]:
    infrastructure_role = _INFRASTRUCTURE_ROLE.search(vacancy.title) is not None
    security_role = _SECURITY_ROLE.search(vacancy.title) is not None
    if not (_OTHER_ROLE.search(vacancy.title) or infrastructure_role or security_role):
        return ()
    profile = " ".join(confirmed_skills)
    clauses = _operating_requirements(
        primary_requirement_clauses(
            vacancy.description or vacancy.responsibilities, vacancy.required_qualifications
        )
        or mandatory_clauses(required_text, include_unlabelled=True),
        "\n".join(
            (
                vacancy.description or "",
                vacancy.required_qualifications or "",
                required_text,
            )
        ),
    )
    gaps: list[str] = []
    for clause in clauses:
        if _CLUSTER.search(clause) and not _confirmed_or_alternative(clause, _CLUSTER, profile):
            production = re.search(
                r"\bproduction[-‑–— ](?:опыт|experience)\b|"
                r"\b(?:опыт|эксплуатац\w*)[^.;\n]{0,80}\bпромышленн\w*\b|"
                r"\bпромышленн\w*[^.;\n]{0,40}\b(?:опыт|эксплуатац\w*)\b",
                clause,
                re.I,
            )
            helm_practice = re.search(r"\bhelm\b", clause, re.I) and _PRACTICE.search(clause)
            if infrastructure_role and (production or helm_practice):
                gaps.append("эксплуатация промышленной инфраструктуры Kubernetes")
            elif _OPERATING_ACTION.search(clause):
                gaps.append("администрирование кластеров Kubernetes")
        if infrastructure_role:
            if (
                _GPU_SERVING.search(clause)
                and _OPERATING_ACTION.search(clause)
                and not _confirmed_or_alternative(clause, _GPU_SERVING, profile)
            ):
                gaps.append(
                    "эксплуатация моделей на GPU"
                    if re.search(r"\bgpu\b", clause, re.I)
                    else "эксплуатация сервисов моделей Triton/vLLM/TensorRT"
                )
            if (
                _INFRASTRUCTURE_CODE.search(clause)
                and (_OPERATING_ACTION.search(clause) or _PRACTICE.search(clause))
                and not _confirmed_or_alternative(clause, _INFRASTRUCTURE_CODE, profile)
            ):
                gaps.append("управление инфраструктурой через Terraform")
        if infrastructure_role and re.search(r"\bgpu\b", clause, re.I):
            resource_management = re.search(
                r"управлени\w*\s+ресурс\w*\s+gpu|"
                r"\b(?:mig|hami|time[-‑ ]slicing|квотирован\w*)\b",
                clause,
                re.I,
            )
            if resource_management and not (
                _CLUSTER.search(profile) and re.search(r"\b(?:gpu|mig|hami)\b", profile, re.I)
            ):
                gaps.append("распределение ресурсов GPU в кластерах")
        if (
            security_role
            and _CRYPTO_TOOLS.search(clause)
            and (_PRACTICE.search(clause) or _OPERATING_ACTION.search(clause))
            and not _confirmed_or_alternative(clause, _CRYPTO_TOOLS, profile)
        ):
            gaps.append("практическая работа со средствами криптографической защиты")
    required_text = " ".join(clauses)
    families = {
        "корпоративные службы каталогов": r"active directory|freeipa|\bald pro\b",
        "корпоративные почтовые серверы": r"dovecot|postfix|ms exchange|microsoft exchange",
        "системы хранения": r"\bceph\b|\biscsi\b|\blvm\b|\bsan\b|\bocfs2\b",
        "системы виртуализации": r"\bvmware\b|\bkvm\b|\bproxmox\b|\bovirt\b|opennebula",
        "корпоративные сети": r"\bvlan\b|\bbgp\b|\bospf\b|\bcisco\b|\bjuniper\b",
    }
    missing = tuple(
        name
        for name, pattern in families.items()
        if re.search(pattern, required_text, re.I) and not re.search(pattern, profile, re.I)
    )
    if len(missing) >= 2:
        gaps.extend(missing)
    return tuple(dict.fromkeys(gaps))
