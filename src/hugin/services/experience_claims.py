# ruff: noqa: RUF001

from __future__ import annotations

import re
from collections.abc import Iterable

_DENIAL = re.compile(
    r"\bопыт\w*\b[^.!?\n]{0,120}\b(?:нет|не\s+имею|не\s+было|отсутству\w*)\b|"
    r"\b(?:нет|не\s+имею|не\s+было|отсутству\w*)\b[^.!?\n]{0,120}\bопыт\w*\b|"
    r"\bне\s+(?:работал(?:а|и)?|работаю|использовал(?:а|и)?|"
    r"применял(?:а|и)?|интегрировал(?:а|и)?|настраивал(?:а|и)?)\b",
    re.IGNORECASE,
)


def _statements(text: str) -> tuple[str, ...]:
    return tuple(
        statement
        for part in re.split(r"\n+|(?<=[.!?;])\s+", text)
        if (statement := " ".join(part.split()))
    )


def _normalized(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w+#]+", text.casefold().replace("ё", "е")))


def unsupported_experience_denial(text: str, facts: Iterable[str]) -> str | None:
    """Require an explicit source statement, preserving the scope of a denial."""
    confirmed = {
        _normalized(statement)
        for fact in facts
        for statement in _statements(fact)
        if _DENIAL.search(statement) is not None
    }
    return next(
        (
            statement
            for statement in _statements(text)
            if _DENIAL.search(statement) is not None and _normalized(statement) not in confirmed
        ),
        None,
    )
