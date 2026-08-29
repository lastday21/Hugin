from __future__ import annotations

from dataclasses import dataclass

import pytest

from hugin.domain.content import MessageDirection
from hugin.services.recruiter_reply_requirement import (
    ReplyRequirement,
    classify_reply_requirement,
)


@dataclass(frozen=True, slots=True)
class Message:
    body: str
    direction: MessageDirection


class RequirementModel:
    model_name = "requirement-test"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.response


@pytest.mark.parametrize(
    ("response", "expected"),
    (
        ("NO_REPLY_REQUIRED", ReplyRequirement.NOT_REQUIRED),
        (" REPLY_REQUIRED\n", ReplyRequirement.REQUIRED),
        ("Ответ не требуется", ReplyRequirement.REQUIRED),
        ("NO_REPLY_REQUIRED, потому что это уведомление", ReplyRequirement.REQUIRED),
    ),
)
def test_requirement_classifier_only_accepts_exact_negative_decision(
    response: str,
    expected: ReplyRequirement,
) -> None:
    model = RequirementModel(response)

    result = classify_reply_requirement(
        model,
        (
            Message("Ранее обсуждали вакансию.", MessageDirection.OUTGOING),
            Message("Давайте пока оставим это здесь.", MessageDirection.INCOMING),
        ),
    )

    assert result is expected
    assert len(model.calls) == 1
    system_prompt, user_prompt = model.calls[0]
    assert "Если есть сомнение" in system_prompt
    assert "Кандидат: Ранее обсуждали вакансию." in user_prompt
    assert "Работодатель: Давайте пока оставим это здесь." in user_prompt
