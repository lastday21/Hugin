from __future__ import annotations

import re
from collections.abc import Iterable

_STACK_LINE = re.compile(
    r"(?im)^\s*(?:технологии|технический стек|стек|technologies|tech stack)\s*:\s*(.+)$"
)
_UNCONFIRMED = re.compile(
    r"\b(?:нет|не|без опыта|отсутств\w*|планир\w*|хочу|готов освоить|"
    r"готов изучить|пока изучаю|no experience|not used|want to learn)\b",
    re.I,
)


def confirmed_skill_texts(facts: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    result: list[str] = []
    for category, content in facts:
        if category in {"skills", "technology"}:
            sources = [content]
        elif category in {"project", "work_experience"}:
            sources = _STACK_LINE.findall(content)
        else:
            continue
        for source in sources:
            for part in re.split(r"[;\n]+", source):
                text = part.strip()
                if text and not _UNCONFIRMED.search(text) and text not in result:
                    result.append(text)
    return tuple(result)
