from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

_NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?")
_BOUNDARY = re.compile(r"[.!?;](?:\s+|$)|\n+")
_UNITS = (
    ("percent", r"\s*(?:%|процент\w*)"),
    ("years", r"\s*(?:лет|год(?:а|ов)?)\b"),
    ("months", r"\s*(?:месяц\w*|мес\.?)(?:\b|$)"),
    ("minutes", r"\s*(?:минут\w*|мин\.?)(?:\b|$)"),
    ("seconds", r"\s*(?:секунд\w*|сек\.?)(?:\b|$)"),
    ("hours", r"\s*(?:час\w*|ч\.)(?:\b|$)"),
    ("days", r"\s*(?:дней|дня|день|суток|сутки)\b"),
    ("rubles", r"\s*(?:рубл\w*|руб\.?|₽|RUB|RUR)(?:\b|$)"),
    ("times", r"\s*(?:раза?|крат\w*)\b"),
)
_METRICS = {
    "revenue": r"выручк|оборот\w* компани|revenue",
    "profit": r"прибыл|profit",
    "accuracy": r"точност|accuracy|точн\w* ответ",
    "requests": r"заяв[ко]|запрос|request",
    "users": r"пользовател|клиент|user",
    "questions": r"вопрос|question",
    "files": r"файл|документ|file",
    "cost": r"затрат|расход|стоимост|cost",
    "salary": r"зарплат|оплат|вознагражден|оклад",
    "duration": r"врем[ея]|перебор|длительност|занима[елт]|duration",
    "experience": r"\b(?:опыт|работал|применя|разраб[ао]т|experience)",
    "throughput": r"производительност|пропускн\w* способност|throughput",
}
_CONDITIONS = {
    "public": r"открыт\w* (?:верси|выборк)|обезличенн\w* верси|публичн",
    "internal": r"внутренн\w* (?:выборк|проверк|тест)",
    "test": r"тестов\w* (?:данн|выборк|стенд)|синтетическ",
    "production": r"промышленн\w* (?:данн|эксплуатац)|\bproduction\b",
    "planned": r"\b(?:планир\w*|целев\w*|планов\w*|ожидаем\w*)",
    "capacity": r"\bрассчитан\w*\s+на\b|расч[её]тн\w*\s+нагруз|проектн\w*\s+мощност",
}
_MONTHS = (
    "январ",
    "феврал",
    "март",
    "апрел",
    "ма[йяе]",
    "июн",
    "июл",
    "август",
    "сентябр",
    "октябр",
    "ноябр",
    "декабр",
)
_PERIOD = re.compile(r"(?:за|в|кажд\w*)\s+(день|сутки|недел\w*|месяц|год|час|минут\w*)\b", re.I)
_TECHNOLOGY = re.compile(
    r"\b(?:python|postgresql|fastapi|django|java|javascript|docker|kubernetes|sql|"
    r"redis|llm|yandexgpt|openai)\b",
    re.I,
)
_PROJECT = re.compile(
    r"\b[Пп]роект\w*\s+(?:[«\"]([^»\"\n]+)[»\"]|([A-Za-z][A-Za-z0-9_-]+|[А-ЯЁ][а-яё]+))"
    r"|\b([A-Za-z][A-Za-z0-9_-]+)\s*[:—]"
)
_QUANTIFIED_CONJUNCTION = re.compile(r",?\s+\b(?:и|а|но)\b\s+|,\s+(?!\d)", re.I)
_SCALED_QUANTITY = re.compile(
    rf"(?P<number>{_NUMBER.pattern})\s*(?P<scale>тыс(?:яч\w*|\.)?|млн\.?|миллион\w*|"
    r"млрд\.?|миллиард\w*)(?!\w)",
    re.I,
)
_QUANTITY_BOUND = re.compile(
    r"(?:\b(не\s+более|не\s+меньше|не\s+менее|не\s+больше|более|больше|менее|меньше|"
    r"свыше|около|примерно|порядка|до)|([<>]=?|[≤≥]))\s*$",
    re.I,
)
_BOUNDS = {
    "не более": "maximum",
    "не больше": "maximum",
    "до": "maximum",
    "не менее": "minimum",
    "не меньше": "minimum",
    "более": "above",
    "больше": "above",
    "свыше": "above",
    "менее": "below",
    "меньше": "below",
    "около": "approximate",
    "примерно": "approximate",
    "порядка": "approximate",
    "<=": "maximum",
    "≤": "maximum",
    ">=": "minimum",
    "≥": "minimum",
    "<": "below",
    ">": "above",
}
_WORD_VALUES: dict[str, int | Decimal] = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
    "тысяча": 1000,
    "тысячи": 1000,
    "тысяч": 1000,
    "тысячу": 1000,
    "двух": 2,
    "трёх": 3,
    "трех": 3,
    "четырёх": 4,
    "четырех": 4,
    "пяти": 5,
    "шести": 6,
    "семи": 7,
    "восьми": 8,
    "девяти": 9,
    "десяти": 10,
    "пятнадцати": 15,
    "двадцати": 20,
    "тридцати": 30,
    "сорока": 40,
    "девяноста": 90,
    "ста": 100,
    "сотня": 100,
    "сотню": 100,
    "сотни": 100,
    "сотней": 100,
    "полтора": Decimal("1.5"),
    "полторы": Decimal("1.5"),
    "полутора": Decimal("1.5"),
    "миллион": 1_000_000,
    "миллиона": 1_000_000,
    "миллионов": 1_000_000,
    "миллиард": 1_000_000_000,
    "миллиарда": 1_000_000_000,
    "миллиардов": 1_000_000_000,
}
_WORD_ALTERNATIVES = "|".join(sorted(_WORD_VALUES, key=len, reverse=True))
_WRITTEN_QUANTITY = re.compile(
    rf"\b(?:{_WORD_ALTERNATIVES})(?:\s+(?:{_WORD_ALTERNATIVES}))*\b"
    r"(?=\s+(?:процент|лет\b|год|месяц|минут|секунд|час|дней\b|дня\b|рубл|"
    r"заяв[ко]|запрос|пользовател|вопрос|файл|документ|раз\b))",
    re.I,
)


def normalize_written_quantities(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if re.search(r"\d\s+$", text[: match.start()]):
            return match.group()
        total = current = Decimal(0)
        for word in match.group().casefold().split():
            value = Decimal(_WORD_VALUES[word])
            if value >= 1000:
                total += (current or Decimal(1)) * value
                current = Decimal(0)
            elif word.startswith("сот"):
                current = (current or Decimal(1)) * value
            else:
                current += value
        return str(total + current)

    def multiply(match: re.Match[str]) -> str:
        scale = match.group("scale").casefold()
        multiplier = (
            1000
            if scale.startswith("тыс")
            else (1_000_000_000 if scale.startswith(("млрд", "миллиард")) else 1_000_000)
        )
        value = Decimal(re.sub(r"\s+", "", match.group("number")).replace(",", "."))
        return format(value * multiplier, "f")

    return _SCALED_QUANTITY.sub(multiply, _WRITTEN_QUANTITY.sub(replace, text))


@dataclass(frozen=True, slots=True)
class NumericClaim:
    value: Decimal
    unit: str
    metric: str | None
    period: str | None
    conditions: frozenset[str]
    technologies: frozenset[str]
    projects: frozenset[str]
    change: str | None
    relation: str | None
    bound: str | None
    negated: bool
    ratio: tuple[Decimal, Decimal] | None
    calendar: tuple[str, ...]
    fragment: str


def _fragments(text: str) -> tuple[str, ...]:
    fragments: list[str] = []
    for sentence in _BOUNDARY.split(text):
        start = 0
        for conjunction in _QUANTIFIED_CONJUNCTION.finditer(sentence):
            if _NUMBER.search(sentence[start : conjunction.start()]) and _NUMBER.search(
                sentence[conjunction.end() :]
            ):
                fragments.append(sentence[start : conjunction.start()].strip())
                start = conjunction.end()
        if sentence[start:].strip():
            fragments.append(sentence[start:].strip())
    return tuple(fragments)


def _claim(fragment: str, number: re.Match[str], source: str) -> NumericClaim:
    after = fragment[number.end() :]
    before = fragment[: number.start()]
    value = Decimal(re.sub(r"[ \u00a0\u202f]", "", number.group()).replace(",", "."))
    unit = next((name for name, pattern in _UNITS if re.match(pattern, after, re.I)), "count")
    if (
        value >= 1900
        and value <= 2100
        and (
            unit == "years"
            or re.match(r"\s*году\b", after, re.I)
            or re.search(
                r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр",
                fragment,
                re.I,
            )
        )
    ):
        unit = "calendar"
    metrics = [
        (min(abs(match.end() - number.start()), abs(match.start() - number.end())), name)
        for name, pattern in _METRICS.items()
        for match in re.finditer(pattern, fragment, re.I)
    ]
    if any(name != "experience" for _, name in metrics):
        metrics = [(distance, name) for distance, name in metrics if name != "experience"]
    metric = min(metrics)[1] if metrics else None
    if unit == "years":
        metric = "experience"
    elif unit == "calendar":
        metric = "calendar"
    period_match = _PERIOD.search(fragment)
    change = None
    if re.search(r"сократ|сни[жз]|уменьш", fragment, re.I):
        change = "decrease"
    elif re.search(r"увелич|повыс|вырос|рост", fragment, re.I):
        change = "increase"
    relation_match = re.search(r"\b(на|до|с|из)\s*$", before, re.I)
    bound_match = _QUANTITY_BOUND.search(before)
    bound_text = next((group for group in bound_match.groups() if group), "") if bound_match else ""
    bound = _BOUNDS[" ".join(bound_text.casefold().split())] if bound_text else None
    if bound_text.casefold() == "до" and change is not None:
        bound = None
    negation_text = _QUANTITY_BOUND.sub("", before.rsplit(",", 1)[-1])
    projects = tuple(_PROJECT.finditer(fragment))
    preceding = tuple(project for project in projects if project.start() < number.start())
    project = preceding[-1] if preceding else (projects[0] if projects else None)
    if project is None:
        project = next(reversed(tuple(_PROJECT.finditer(source))), None)
    ratio = None
    for pair in re.finditer(
        rf"(?P<part>{_NUMBER.pattern})[^\d.!?;\n]*?\bиз\s+(?P<total>{_NUMBER.pattern})",
        fragment,
        re.I,
    ):
        if pair.start() <= number.start() < pair.end():
            ratio = (
                Decimal(re.sub(r"\s+", "", pair.group("part")).replace(",", ".")),
                Decimal(re.sub(r"\s+", "", pair.group("total")).replace(",", ".")),
            )
            break
    return NumericClaim(
        value=value,
        unit=unit,
        metric=metric,
        period=period_match.group(1).casefold() if period_match else None,
        conditions=frozenset(k for k, p in _CONDITIONS.items() if re.search(p, fragment, re.I)),
        technologies=frozenset(m.group().casefold() for m in _TECHNOLOGY.finditer(fragment)),
        projects=frozenset({next(group for group in project.groups() if group).casefold()})
        if project
        else frozenset(),
        change=change,
        relation=relation_match.group(1).casefold() if relation_match else None,
        bound=bound,
        negated=(
            re.search(r"\bне\b(?!\s+только\b)", negation_text, re.I) is not None
            or re.search(
                r"\bне\s+(?:обраб|получ|достиг|увелич|повыс|сократ|сни[жз]|уменьш|"
                r"созда|сдел|разраб|работ|примен|использ)",
                after,
                re.I,
            )
            is not None
        ),
        ratio=ratio,
        calendar=tuple(
            match.group(1)
            for match in re.finditer(r"\b((?:19|20)\d{2})\s*(?:год\w*|г\.)", fragment, re.I)
        )
        + tuple(
            f"month:{index}"
            for index, month in enumerate(_MONTHS, 1)
            if re.search(rf"\b(?:{month})\w*\b", fragment, re.I)
        ),
        fragment=fragment,
    )


def _is_identifier(fragment: str, number: re.Match[str], allowed_text: str) -> bool:
    if any(re.match(pattern, fragment[number.end() :], re.I) for _, pattern in _UNITS):
        return False
    if re.match(
        r"\s*(?:сервис|бот|проект|приложен|систем|модул|заяв[ко]|запрос|клиент|"
        r"пользовател|вопрос|файл|документ)",
        fragment[number.end() :],
        re.I,
    ):
        return False
    for token in re.finditer(
        r"\b(?:python|postgresql|fastapi|django|java|http|oauth)\s+\d+(?:\.\d+)*\b",
        fragment,
        re.I,
    ):
        if token.start() <= number.start() and number.end() <= token.end():
            normalized = re.sub(r"\s+", " ", token.group()).casefold()
            if normalized in re.sub(r"\s+", " ", allowed_text).casefold():
                return True
    return False


def _supported(claim: NumericClaim, source: NumericClaim) -> bool:
    if (claim.bound, claim.negated, claim.ratio, claim.calendar) != (
        source.bound,
        source.negated,
        source.ratio,
        source.calendar,
    ):
        return False
    if (claim.value, claim.unit, claim.metric, claim.period, claim.conditions) != (
        source.value,
        source.unit,
        source.metric,
        source.period,
        source.conditions,
    ):
        return False
    if claim.metric is None:
        return claim.fragment.casefold() == source.fragment.casefold()
    if claim.projects and not claim.projects.issubset(source.projects):
        return False
    if claim.technologies and not claim.technologies.issubset(source.technologies):
        return False
    if claim.change is not None and claim.change != source.change:
        return False
    if claim.unit == "percent" and claim.change != source.change:
        return False
    if claim.relation == "на" and source.relation != "на":
        return False
    if (claim.relation == "из") != (source.relation == "из"):
        return False
    return not (claim.relation == "с" and source.relation == "до")


def _claims(text: str) -> tuple[NumericClaim, ...]:
    claims: list[NumericClaim] = []
    project_context = ""
    for fragment in _qualified_fragments(text):
        if _PROJECT.search(fragment):
            project_context = fragment
        claims.extend(
            _claim(fragment, number, project_context) for number in _NUMBER.finditer(fragment)
        )
    return tuple(claims)


def _qualified_fragments(text: str) -> tuple[str, ...]:
    result: list[str] = []
    qualifier = ""
    for fragment in _fragments(text):
        heading = re.sub(r"^(?:[-•]\s*|\d+[.)]\s*)", "", fragment).rstrip()
        if heading.endswith(":"):
            if _PROJECT.search(heading):
                qualifier = ""
            if any(re.search(pattern, heading, re.I) for pattern in _CONDITIONS.values()) or any(
                re.search(rf"\b(?:{month})\w*\b", heading, re.I) for month in _MONTHS
            ):
                qualifier = heading
                result.append(fragment)
                continue
        result.append(f"{qualifier} {fragment}" if qualifier else fragment)
    return tuple(result)


def unsupported_numeric_claim(
    text: str, sources: tuple[str, ...], *, identifier_context: str = ""
) -> str | None:
    text = normalize_written_quantities(text)
    sources = tuple(normalize_written_quantities(source) for source in sources)
    evidence = tuple(item for source in sources for item in _claims(source))
    project_context = ""
    for fragment in _qualified_fragments(text):
        if _PROJECT.search(fragment):
            project_context = fragment
        for number in _NUMBER.finditer(fragment):
            if _is_identifier(fragment, number, "\n".join((*sources, identifier_context))):
                continue
            claim = _claim(fragment, number, project_context)
            if not any(_supported(claim, item) for item in evidence):
                return fragment
    return None
