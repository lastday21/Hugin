from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from hugin.domain.vacancies import VacancyData
from hugin.services.requirement_sections import (
    RequirementKind,
    primary_duties,
    requirement_sections,
)


class TechnicalDuty(StrEnum):
    DEVELOPMENT = "development"
    OPERATIONS = "operations"
    DATA = "data"


@dataclass(frozen=True, slots=True)
class DutyEvidence:
    kind: TechnicalDuty
    text: str


_PATTERNS = (
    (
        TechnicalDuty.DEVELOPMENT,
        re.compile(
            r"\b(?:разработ\w*|разрабат\w*|созда\w*|реализ\w*|develop\w*|implement\w*)"
            r"[^.!?;\n]{0,100}\b(?:сервис\w*|приложени\w*|api|программ(?!ист)\w*|"
            r"скрипт\w*|ии|ai|llm|бот\w*|автотест\w*)\b",
            re.I,
        ),
    ),
    (
        TechnicalDuty.OPERATIONS,
        re.compile(
            r"\b(?:администр\w*|сопровожд\w*|поддерж\w*|монитор\w*|диагност\w*|"
            r"анализ\w*|управлени\w*|управля\w*|настра\w*|настрой\w*|восстанов\w*)"
            r"[^.!?;\n]{0,100}\b(?:linux|windows|сервер\w*|лог\w*|"
            r"программ\w* обеспечени\w*|информационн\w* систем\w*|"
            r"бд|api|postgresql|docker)\b",
            re.I,
        ),
    ),
    (
        TechnicalDuty.DATA,
        re.compile(
            r"\b(?:разработ\w*|разрабат\w*|созда\w*|поддерж\w*|автоматиз\w*|настра\w*|"
            r"проектир\w*|развива\w*|развит\w*)"
            r"[^.!?;\n]{0,100}\b(?:etl|elt|пайплайн\w*|sql|"
            r"(?:загрузк\w*|преобразован\w*|обработк\w*)\s+"
            r"(?:(?:больш\w*|объ[её]м\w*)\s+){0,2}данных|"
            r"(?:аналитическ\w*\s+)?витрин\w*\s+данных|"
            r"(?:архитектур\w*|хранилищ\w*)\s+данных)\b",
            re.I,
        ),
    ),
    (
        TechnicalDuty.DATA,
        re.compile(
            r"\b(?:автоматиз\w*)[^.!?;\n]{0,60}\b(?:отч[её]т\w*)"
            r"[^.!?;\n]{0,60}\b(?:скрипт\w*|python|sql|vba|power\s+bi|tableau)\b",
            re.I,
        ),
    ),
    (
        TechnicalDuty.DATA,
        re.compile(
            r"\b(?:подготов\w*)\s+скрипт\w*[^.!?;\n]{0,60}"
            r"\b(?:загрузк\w*|обработк\w*|преобразован\w*)\s+данных\b",
            re.I,
        ),
    ),
)
_OTHER_ACTOR = re.compile(
    r"\b(?:который|которая|которое|которые|они|команда|отдел)\s+(?:\w+\s+){0,3}$", re.I
)
_RELATIVE_CLAUSE = re.compile(r"\b(?:который|которая|которое|которые)\b", re.I)
_NEGATED_ACTION = re.compile(r"\b(?:не)\s+(?!только\b)(?:\w+\s+){0,3}$", re.I)
_NEGATED_AFTER_ACTION = re.compile(
    r"^\s+(?:не)\s+(?:требуется|нужно|планируется|предстоит|прид[её]тся)\b", re.I
)
_TEAM_REFERENCE = re.compile(r"\b(?:команд\w*|отдел\w*)\s*$", re.I)
_PARTICIPLE = re.compile(r"\w*(?:ющ|ущ|вш)\w*", re.I)
_DELEGATED_ACTION = re.compile(
    r"\b(?:(?:выполня|осуществля)(?:ет|ют)(?:ся)?|занима(?:ет|ют)ся|будет|будут)\b"
    r"[^.!?;\n]{0,40}"
    r"\b(?:друг\w*\s+)?(?:команд\w*|отдел\w*|разработчик\w*|программист\w*)\b",
    re.I,
)


def _candidate_action(clause: str, match: re.Match[str]) -> bool:
    before = clause[: match.start()]
    action_word = match.group().split()[0]
    after = re.split(r"[,;.!?]", clause[match.end() :], maxsplit=1)[0]
    if _OTHER_ACTOR.search(before) or _RELATIVE_CLAUSE.search(match.group()):
        return False
    if _PARTICIPLE.fullmatch(action_word):
        return False
    if re.fullmatch(r"разработк\w*|развити\w*", action_word, re.I) and _TEAM_REFERENCE.search(
        before
    ):
        return False
    if _NEGATED_ACTION.search(before) or _NEGATED_AFTER_ACTION.search(after):
        return False
    return not _DELEGATED_ACTION.search(after)


def technical_duty_evidence(vacancy: VacancyData) -> tuple[DutyEvidence, ...]:
    if not vacancy.responsibilities and not any(
        section.kind is RequirementKind.DUTIES
        for section in requirement_sections(vacancy.description or "")
    ):
        return ()
    duties = primary_duties(vacancy.description, vacancy.responsibilities)
    result: list[DutyEvidence] = []
    for clause in re.split(r"[\n;]+", duties):
        clause = clause.strip()
        for kind, pattern in _PATTERNS:
            if any(_candidate_action(clause, match) for match in pattern.finditer(clause)):
                result.append(DutyEvidence(kind, clause))
    return tuple(result)
