from __future__ import annotations

import re
from collections.abc import Sequence

INDUSTRY_EXCLUSIONS = re.compile(r"(?:сфер\w*|отрасл\w*)[^?]{0,120}не\s+рассматрива", re.IGNORECASE)


def is_fixed_choice(field_type: str, options: Sequence[str]) -> bool:
    return field_type.casefold() == "checkbox" or (
        field_type.casefold() in {"radio", "select"} and bool(options)
    )


def sensitive_question_text(question: str) -> str:
    if INDUSTRY_EXCLUSIONS.search(question):
        return re.sub(r"\bбанки\b", "", question, flags=re.IGNORECASE)  # noqa: RUF001
    return question
