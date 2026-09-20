# ruff: noqa: RUF001

from __future__ import annotations

import re

# Удаляются только вводные слова; условия вопроса должны остаться в подписи.
_INTRO = re.compile(
    r"\b(?:какой|какая|какие|какое|каков|какова|каковы|как|где|вы|ваш|ваша|ваши|"
    r"ваше|вашу|вашего|вашей|ваших|у|вас|твой|твоя|твои|твое|тебя|ты|"
    r"укажи|укажите|уточните|напиши|напишите|пожалуйста|свой|своя|свои|свое|"
    r"в|на|из|и|по|за|с|о|об)\b"
)
_SALARY = re.compile(r"зарплат\w*|заработн\w*\s+плат\w*|оклад\w*")
_GROSS = re.compile(r"до\s+вычета\s+(?:налог\w*|ндфл)|\bgross\b|\bгросс\b")
_NET = re.compile(r"после\s+вычета\s+(?:налог\w*|ндфл)|на\s+руки|\bnet\b|\bнетто\b")
_SALARY_NEUTRAL = re.compile(
    r"\b(?:ожидани\w*|ожида\w*|желаем\w*|вилк\w*|диапазон\w*|сумм\w*|"
    r"размер\w*|составля\w*|стабильн\w*|расчет\w*|хотели|бы|получать)\b"
)
_SALARY_QUALIFIERS = (
    ("minimum", r"\bминимальн\w*\b"),
    ("hour", r"\bчас\w*\b"),
    ("month", r"\bмесяц\w*\b|\bежемесячн\w*\b"),
    ("year", r"\bгод\w*\b|\bежегодн\w*\b"),
    ("rub", r"\bруб\w*\b|₽"),
    ("usd", r"\bдоллар\w*\b|\busd\b|\$"),
    ("eur", r"\bевро\b|\beur\b|€"),
)
_SALARY_PERIODS = (
    ("hour", r"\b(?:в|за)\s+час\b|/\s*час\b|\bпочасов\w*"),
    ("month", r"\b(?:в|за)\s+месяц\b|/\s*месяц\b|\bежемесячн\w*"),
    ("year", r"\b(?:в|за)\s+год\b|/\s*год\b|\bежегодн\w*"),
)


def reusable_question_key(question: str) -> tuple[str, ...] | None:
    """Подпись простого вопроса; неизвестные условия запрещают перенос ответа."""
    text = question.casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s$€₽]", " ", text)
    text = " ".join(text.split())
    if _SALARY.search(text):
        gross, net = bool(_GROSS.search(text)), bool(_NET.search(text))
        if gross and net:
            return None
        basis = "gross" if gross else "net" if net else "unspecified"
        text = _GROSS.sub(" ", _NET.sub(" ", _SALARY.sub(" ", text)))
        qualifiers = []
        for name, pattern in _SALARY_QUALIFIERS:
            if re.search(pattern, text):
                qualifiers.append(name)
                text = re.sub(pattern, " ", text)
        text = _SALARY_NEUTRAL.sub(" ", text)
        if _INTRO.sub(" ", text).strip():
            return None
        return ("salary_expectation", basis, *qualifiers)
    for category, pattern in (
        ("email", r"электронн\w*\s+почт\w*|\bemail\b|\be mail\b"),
        ("phone", r"(?:номер\s+)?телефон\w*"),
        ("telegram", r"(?:ник\w*\s+)?(?:телеграм\w*|telegram)"),
        ("github", r"(?:ссылк\w*\s+)?(?:профил\w*\s+)?github"),
        ("location", r"город\w*\s+проживани\w*|мест\w*\s+жительств\w*"),
        ("full_name", r"\bфио\b|фамили\w*\s+имя\s+отчеств\w*"),
    ):
        if re.search(pattern, text) and not _INTRO.sub(" ", re.sub(pattern, " ", text)).strip():
            return (category,)
    return None


def salary_profile_answer_is_compatible(question: str, answer: str) -> bool:
    key = reusable_question_key(question)
    if key is None or key[0] != "salary_expectation":
        return False
    content = answer.casefold().replace("ё", "е")
    if (key[1] == "net" and not _NET.search(content)) or (
        key[1] == "gross" and not _GROSS.search(content)
    ):
        return False
    requested_periods = set(key[2:]) & {"hour", "month", "year"}
    if requested_periods and requested_periods != {
        name for name, pattern in _SALARY_PERIODS if re.search(pattern, content)
    }:
        return False
    requested_currencies = set(key[2:]) & {"rub", "usd", "eur"}
    if requested_currencies and requested_currencies != {
        name
        for name, pattern in _SALARY_QUALIFIERS
        if name in {"rub", "usd", "eur"} and re.search(pattern, content)
    }:
        return False
    return all(
        re.search(pattern, content) is not None
        for name, pattern in _SALARY_QUALIFIERS
        if name == "minimum" and name in key[2:]
    )
