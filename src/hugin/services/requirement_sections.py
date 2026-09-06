from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_OPTIONAL_SIGNAL = (
    r"(?:буд(?:ет|ут)\s+(?:являться\s+)?"
    r"(?:(?:существенн|больш|дополнительн|значительн)\w*\s+)?"
    r"(?:плюсом|преимуществом)|(?:плюсом|преимуществом)\s+будет)"
)

REQUIRED_HEADING = (
    r"(?:основные\s+технологии)|"
    "(?:(?:основные\\s+)?требования(?:\\s+к\\s+кандидату)?|"
    "мы\\s+ожидаем|нам\\s+важно|(?:что\\s+)?требуется|что\\s+жд[её]м|"
    "ожидания\\s+от\\s+квалификации|"
    "(?:мы\\s+)?(?:ожидаем|жд[её]м)\\s+от\\s+(?:вас|тебя|кандидата|"
    "(?:будущего|нового)\\s+(?:члена\\s+команды|коллеги|сотрудника))|"
    "кандидат\\s+нам\\s+подходит\\s+если|"
    r"(?:ты|вы|кандидат)\s+нам\s+подход\w*,?\s+если|"
    "ключевые\\s+навыки|requirements|required\\s+qualifications|must\\s+have|"
    "что\\s+мы\\s+жд[её]м[^:\\n]*|"
    "что\\s+мы\\s+ожидаем(?:\\s+от\\s+кандидат\\w*)?|"
    "жд[её]м\\s+от\\s+тебя|"
    "мы\\s+ожидаем\\s+от\\s+тебя|"
    "мы\\s+жд[её]м\\s+от\\s+вас|"
    "что\\s+ожидаем\\s+от\\s+кандидата|"
    "будем\\s+рады\\s+видеть[^:\\n]*|"
    "для\\s+нас\\s+важно|"
    "что\\s+важно\\s+для\\s+нас|"
    "чего\\s+мы\\s+ожидаем|"
    "ожидания|"
    "наши\\s+ожидания|"
    "наш[и]\\s+пожелания\\s+к\\s+кандидатам|"
    "опыт\\s+и\\s+навыки|"
    "кого\\s+мы\\s+ищем|"
    "что\\s+для\\s+этого\\s+необходимо|"
    "технические\\s+требования|"
    "какой\\s+опыт\\s+и\\s+знания\\s+нужны|"
    "что\\s+нужно\\s+уметь|"
    "мы\\s+ищем\\s+(?:разработчика|"
    "кандидата)[^:\\n]*|"
    "ты\\s*[-–—]?\\s*(?:тот|"
    "та)\\s+сам\\w*[^:\\n]*|"
    "пожелания\\s+к\\s+кандидат\\w*|"
    "обязательн\\w*\\s+требован\\w*(?:\\s*\\(\\s*must\\s+have\\s*\\))?|"
    "обязательно(?:\\s*\\(\\s*must\\s+have\\s*\\))?|"
    "стек\\s*\\(?(?:обязательно|"
    "обязательный)\\)?|"
    "(?:тот|"
    "та|"
    "кандидат)[^:\\n]{0,160}\\bимеет)"
)
OPTIONAL_HEADING = (
    rf"(?:{_OPTIONAL_SIGNAL}(?:\s+и\s+[^:.\n]{{1,60}})?|"
    "желательн\\w*\\s+навык\\w*(?:\\s*\\(будет\\s+плюсом\\))?|"
    "желательно|"
    "ст[еэ]к\\s+желательн\\w*|"
    "необязательно|"
    "приветству\\w*|"
    "optional|"
    "preferred|"
    "nice\\s+to\\s+have|"
    "условия|"
    "мы\\s+предлагаем|"
    "что\\s+мы\\s+предлагаем|"
    "что\\s+мы\\s+можем(?:\\s+(?:вам|"
    "тебе))?\\s+гарантировать|"
    "что\\s+мы\\s+гарантируем|"
    "предлагаем)"
)

_OFFER_HEADING = (
    r"(?:условия(?:\s+(?:работы|и преимущества))?|(?:что\s+)?мы\s+предлагаем|предлагаем(?:\s+вам)?)"
)
_DUTY_HEADING = (
    r"(?:обязанности|(?:(?:основные|предстоящие|ваши|твои)\s+)?задачи|"
    r"чем\s+(?:предстоит|(?:(?:ты|вы)\s+)?будешь|(?:(?:ты|вы)\s+)?будете)\s+заниматься|"
    r"что\s+(?:предстоит|нужно(?:\s+будет)?|(?:(?:ты|вы)\s+)?будешь|"
    r"(?:(?:ты|вы)\s+)?будете)\s+делать|вы будете)"
)
_HEADING = re.compile(
    rf"(?<!\w)(?:(?P<offer>{_OFFER_HEADING})|"
    rf"(?P<optional>{OPTIONAL_HEADING})|(?P<required>{REQUIRED_HEADING})|"
    rf"(?P<duties>{_DUTY_HEADING}))(?:[ \t]*\([^():\n]{{1,100}}\))?"
    r"[ \t]*(?::[ \t]*|(?=\r?$))",
    re.IGNORECASE | re.MULTILINE,
)
_OPTIONAL_CLAUSE = re.compile(
    rf"\b(?:{_OPTIONAL_SIGNAL}|желательн\w*|желателен|необязательн\w*|необязателен|"
    r"приветству\w*|не\s+(?:обязател\w*|требу\w*|нуж\w*|необходим\w*))\b",
    re.IGNORECASE,
)
_EXPLICIT_REQUIRED = re.compile(r"\b(?:обязател\w*|необходим\w*|требуется)\b", re.I)
_CLAUSE_BOUNDARY = re.compile(
    r"[\n;]+|(?<!\d\.)(?<=[.!?])[ \t]+|"
    r",[ \t]*(?=(?:(?:а|но)\s+|"
    r"(?:обязател\w*|необходим\w*|требуется|знани\w*|понимани\w*|опыт|умени\w*)\b))",
    re.IGNORECASE,
)


class RequirementKind(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    OFFER = "offer"
    DUTIES = "duties"
    UNLABELLED = "unlabelled"


@dataclass(frozen=True, slots=True)
class RequirementSection:
    kind: RequirementKind
    text: str
    start: int
    end: int


def requirement_sections(text: str) -> tuple[RequirementSection, ...]:
    sections: list[RequirementSection] = []
    kind = RequirementKind.UNLABELLED
    start = 0
    for heading in _HEADING.finditer(text):
        # Слова внутри строки относятся только к текущему пункту.
        if not heading.group().rstrip().endswith(":"):
            line_start = text.rfind("\n", 0, heading.start()) + 1
            if text[line_start : heading.start()].strip(" \t\r-—–•#*"):
                continue
        if text[start : heading.start()].strip():
            sections.append(
                RequirementSection(kind, text[start : heading.start()], start, heading.start())
            )
        kind = RequirementKind(heading.lastgroup or "unlabelled")
        start = heading.end()
    if text[start:].strip():
        sections.append(RequirementSection(kind, text[start:], start, len(text)))
    return tuple(sections)


def mandatory_clauses(text: str, *, include_unlabelled: bool) -> tuple[str, ...]:
    # Нумерованные требования после желательного стека возобновляют основной список.
    text = re.sub(
        r"(?is)(?<!\w)ст[еэ]к\s+желательн\w*\s*:\s*.*?"
        r"(?=\s*\d+[.)]\s*(?:опыт\w*|знан\w*|умени\w*|понимани\w*|"
        r"владени\w*|навык\w*|способност\w*))",
        "\nТребования:\n",
        text,
    )
    allowed = {RequirementKind.REQUIRED}
    if include_unlabelled:
        allowed.update((RequirementKind.UNLABELLED, RequirementKind.DUTIES))
    clauses = (
        clause
        for section in requirement_sections(text)
        for clause in _CLAUSE_BOUNDARY.split(section.text)
        if section.kind in allowed
        or (section.kind is RequirementKind.OPTIONAL and _EXPLICIT_REQUIRED.search(clause))
        if not _OPTIONAL_CLAUSE.search(clause)
    )
    return tuple(
        re.sub(r"\s+", " ", clause).strip().casefold() for clause in clauses if clause.strip()
    )


def mandatory_text(text: str, *, include_unlabelled: bool) -> str:
    return " ".join(mandatory_clauses(text, include_unlabelled=include_unlabelled))


def description_sections(text: str) -> tuple[str | None, str | None, str | None]:
    sections = requirement_sections(text)
    values = {
        kind: "\n".join(section.text.strip() for section in sections if section.kind is kind)
        or None
        for kind in (RequirementKind.DUTIES, RequirementKind.REQUIRED, RequirementKind.OPTIONAL)
    }
    return (
        values[RequirementKind.DUTIES],
        values[RequirementKind.REQUIRED],
        values[RequirementKind.OPTIONAL],
    )


def primary_requirement_clauses(description: str | None, fallback: str | None) -> tuple[str, ...]:
    sections = requirement_sections(description or "")
    required = tuple(section for section in sections if section.kind is RequirementKind.REQUIRED)
    if required:
        return mandatory_clauses(description or "", include_unlabelled=False)
    if fallback:
        compact = re.sub(r"\s+", " ", fallback).strip().casefold()
        if any(
            section.kind in {RequirementKind.OFFER, RequirementKind.OPTIONAL}
            and compact in re.sub(r"\s+", " ", section.text).strip().casefold()
            for section in sections
        ):
            return ()
        return mandatory_clauses(fallback, include_unlabelled=True)
    return mandatory_clauses(description or "", include_unlabelled=False)


def primary_requirements(description: str | None, fallback: str | None) -> str:
    return " ".join(primary_requirement_clauses(description, fallback))


def primary_duties(description: str | None, fallback: str | None) -> str:
    sections = requirement_sections(description or "")
    duties = "\n".join(
        section.text.strip() for section in sections if section.kind is RequirementKind.DUTIES
    )
    if duties:
        return duties
    if fallback:
        compact_fallback = re.sub(r"\s+", " ", fallback).strip().casefold()
        if not any(
            section.kind in {RequirementKind.OFFER, RequirementKind.OPTIONAL}
            and compact_fallback in re.sub(r"\s+", " ", section.text).strip().casefold()
            for section in sections
        ):
            return fallback
    return mandatory_text(description or "", include_unlabelled=True)
