from __future__ import annotations

import re

INDUSTRY_EXCLUSIONS = re.compile(r"(?:сфер\w*|отрасл\w*)[^?]{0,120}не\s+рассматрива", re.IGNORECASE)


def sensitive_question_text(question: str) -> str:
    if INDUSTRY_EXCLUSIONS.search(question):
        return re.sub(r"\bбанки\b", "", question, flags=re.IGNORECASE)  # noqa: RUF001
    return question
